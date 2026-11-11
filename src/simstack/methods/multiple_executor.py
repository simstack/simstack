import asyncio
import logging
from typing import List, Callable, TypeVar, Generic, Dict, Any, AsyncGenerator, Union

from simstack.models.document import Model, Field, ObjectId, Reference

from simstack.core.asnyc_helper import async_helper
from simstack.core.context import context
from simstack.core.node import node
from simstack.models import (
    simstack_model,
    ModelMapping,
    BinaryOperationInput,
    FloatData,
)
from simstack.util.async_zip_utils import async_zip
from simstack.util.importer import import_function, import_class
from simstack.util.make_table import make_table_entries
from simstack.server_api.http_transport import RunnerAPIError

# Define your prefix
PREFIX = "[ParallelExecutor]"


@node
def multiple_executor_adder(
    arg1: BinaryOperationInput, arg2: BinaryOperationInput
) -> BinaryOperationInput:
    return BinaryOperationInput(arg1=arg1.arg1 + arg2.arg1, arg2=arg1.arg2 + arg2.arg2)


class PrefixFilter(logging.Filter):
    def filter(self, record):
        record.msg = f"{PREFIX} {record.msg}"
        return True


# Set up the logger
logger = logging.getLogger(__name__)
logger.addFilter(PrefixFilter())


@async_helper
async def get_mappings(func: Callable[[Model], Model]):
    """
    Decorator to map a function to a model.
    """
    node_model = next((item for item in await context.server.values.catalogue_nodes() if item.name == func.__name__), None)
    if node_model is None:
        raise ValueError(f"Node model {func.__name__} not found in the catalogue.")

    # Get return type annotation from function
    return_type = func.__annotations__.get("return")
    if return_type is None:
        raise ValueError(f"Function {func.__name__} must have return type annotation")

    result_model = next((item for item in await context.server.values.catalogue_models() if item.name == return_type.__name__), None)
    if result_model is None:
        raise ValueError(f"Result model {return_type.__name__} not found in the catalogue.")

    return node_model.function_mapping, node_model.input_mappings, result_model.mapping


T = TypeVar("T", bound=Model)
R = TypeVar("R", bound=Model)


def _prefix_dict_keys(original_dict, prefix):
    """Helper method to prefix dictionary keys."""
    return {f"{prefix}{key}": value for key, value in original_dict.items()}


async def _get_formatted_column_defs(mapping, prefix):
    """Get column definitions for a model with proper field prefixes."""
    model_class = await import_class(mapping)
    column_defs = model_class.make_column_defs()

    # Add prefix to field names
    for col in column_defs:
        if "field" in col:
            col["field"] = f"{prefix}{col['field']}"

    # Filter out id fields
    return [col for col in column_defs if col.get("field") != f"{prefix}id"]


@simstack_model
class MultipleExecutorInput(Model, Generic[T]):
    """
    Represents a MultipleExecutor which handles multiple calculations in parallel and aggregates the results.
    # TODO store the inputs as ids not in memory
    """

    function_mapping: str
    input_mappings: List[str]
    result_mapping: str
    timeout: int = 600
    inputs_data: List[List[ObjectId]] = Field(default_factory=list)

    @classmethod
    def from_function(cls, func: Callable[[T], R]):
        function_mapping, input_mappings, result_mapping = get_mappings(func)
        print(
            f"Function mapping: {function_mapping}, Input mappings: {input_mappings}, Result mapping: {result_mapping}"
        )
        return cls(
            function_mapping=function_mapping,
            input_mappings=input_mappings,
            result_mapping=result_mapping,
        )

    async def input_generator(self) -> AsyncGenerator[List[Model], None]:
        cache = vars(self).get("_cache")
        if cache is None:
            raise ValueError("Resolve executor inputs explicitly before reading")
        for row in cache:
            yield row

    async def append(self, input_models: Union[T, List[T]]):
        """Add the inputs for a single function call"""
        if isinstance(input_models, Model):
            input_models = [input_models]
        cache = vars(self).setdefault("_cache", [])
        if len(cache) != len(self.inputs_data):
            raise ValueError("Resolve executor inputs explicitly before editing")
        self.inputs_data.append([model.id for model in input_models])
        cache.append(list(input_models))

    async def execute(self, **kwargs) -> "MultipleExecutorOutput":
        task_id = kwargs.get("task_id", "NA")
        global PREFIX
        if task_id != "NA":
            PREFIX = f"[ParallelExecutor task_id: {task_id}]"

        # Prepare all jobs to run concurrently
        tasks = []
        result_ids = [None] * len(self.inputs_data)
        result_models = [None] * len(self.inputs_data)
        kwargs["parent_id"] = task_id
        try:
            func = await import_function(self.function_mapping, context.server.values, task_id)
            i = 0
            async for task_input in self.input_generator():
                # Create an async task that calls our async wrapper
                task = asyncio.create_task(func(*task_input, **kwargs))
                # Store index for later reference
                task.index = i
                tasks.append(task)
                i += 1
        except RunnerAPIError:
            raise
        except Exception as e:
            logger.error(f"Error preparing tasks: {str(e)}")

        # Wait for all tasks with timeout
        done, pending = await asyncio.wait(tasks, timeout=self.timeout)

        # Process completed tasks
        for task in done:
            try:
                task_result = task.result()
                result_ids[task.index] = task_result.id
                result_models[task.index] = task_result
                logger.info(f"Successfully processed task {task.index}")
            except RunnerAPIError:
                raise
            except Exception as e:
                logger.error(f"Error processing task {task.index}: {str(e)}")

        # Log information about pending tasks
        if pending:
            logger.error(f"{len(pending)} tasks didn't complete within the timeout")
            for task in pending:
                logger.error(
                    f"Task at index {task.index} didn't complete within timeout"
                )

        output = MultipleExecutorOutput(
            executor_input=self,
            result_mapping=self.result_mapping,
            result_ids=result_ids,
        )
        object.__setattr__(output, "_cache", result_models)
        logger.info("finished")
        return output

    @classmethod
    def ui_base_schema(cls):
        return {"inputs_data": {"ui:widget": "hidden"}}


@simstack_model
class MultipleExecutorOutput(Model, Generic[T, R]):
    """
    Represents the output of a MultipleExecutor.
    """

    executor_input: MultipleExecutorInput = Reference()
    result_mapping: str  # there is one result: either a Model or a SimstackResult
    result_ids: List[Union[ObjectId, None]] = Field(default_factory=list)

    @classmethod
    def json_schema(cls):
        return {
            "type": "object",
            "properties": {"results": {"type": "array", "items": {"type": "object"}}},
            "required": ["result_mapping", "results"],
        }

    def custom_model_dump(self) -> Dict[str, Any]:
        cache = vars(self).get("_cache")
        if cache is None:
            raise ValueError("Resolve executor results explicitly before presentation")
        return {"result": [item.model_dump() if item is not None else None for item in cache]}

    async def result_generator(self) -> AsyncGenerator[Union[R, None], None]:
        cache = vars(self).get("_cache")
        if cache is None:
            raise ValueError("Resolve executor results explicitly before reading")
        for item in cache:
            yield item

    @async_helper
    async def make_table(self):
        """
        Create a table representation of the results with formatted columns.
        Returns a dictionary with table data and column definitions.
        """
        # Get inputs and results data
        inputs = await self.get_inputs()
        results = await self.get_results()

        # Create tables for inputs and results
        inputs_table = make_table_entries(inputs)
        results_table = make_table_entries(results)

        # Merge data based on finished indices
        merged_entries = []
        for i, input_entry in enumerate(inputs_table["rowData"]):
            entry = {**input_entry}
            if i in self.finished_indices:
                result_idx = self.finished_indices.index(i)
                entry.update(results_table["rowData"][result_idx])
            # TODO find all artifacts to this result and add the data to the entry
            merged_entries.append(entry)

        # Combine column definitions
        merged_columns = inputs_table["columnDefs"] + results_table["columnDefs"]

        return {"rowData": merged_entries, "columnDefs": merged_columns}


@node
async def multiple_executor(
    executor: MultipleExecutorInput, **kwargs
) -> MultipleExecutorOutput:
    return await executor.execute(**kwargs)


@node
async def async_adder(arg: BinaryOperationInput, **kwargs) -> FloatData:
    return FloatData(value=arg.arg1 + arg.arg2)


async def main():
    context.initialize()
    executor_input = MultipleExecutorInput.from_function(async_adder)
    # Create a list of input models
    inputs = [BinaryOperationInput(arg1=2, arg2=value) for value in range(1, 3)]

    # Append the input models to the executor
    for input_model in inputs:
        await executor_input.append(input_model)

    executor_output = await multiple_executor(executor_input)

    print("Results: ", executor_output)
    async for item1, item2 in async_zip(
        executor_input.input_generator(), executor_output.result_generator()
    ):
        print(f"Input: {item1}, Result: {item2}")


if __name__ == "__main__":
    asyncio.run(main())
