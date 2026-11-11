import asyncio
from typing import List, Callable, Any, Optional

from simstack.models.document import Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import compute_arg_hash
from simstack.core.simstack_result import SimstackResult
from simstack.core.node_runner import NodeRunner
from simstack.core.process_results import process_result_helper
from simstack.models import DataSetMetadata, DataSet, DataSetSection, BooleanData, StringData
from simstack.server_api.http_transport import RunnerAPIError

class MassRunner(NodeRunner):
    def __init__(self, node: Callable[..., Any],max_concurrency: Optional[int] = None, **kwargs):
        super().__init__(kwargs["node_runner"].name, kwargs["node_runner"].task_id, kwargs["node_runner"].logger )
        node_runner = kwargs["node_runner"]
        arg_hash = kwargs["arg_hash"]
        self._kwargs = kwargs.copy()


        database_metadata = DataSetMetadata(
            field_name=node_runner.name,
            data={
                "arg_hash": str(arg_hash),
                "task_id": str(node_runner.task_id),
                "call_path": getattr(node_runner, "call_path", "NA"),
            }
        )

        self._max_concurrency = max_concurrency

        self.dataset = DataSet(
            field_name=f"{node_runner.name}.{self.task_id}",
            metadata=database_metadata
        )
        self._existing_dataset = None
        self._failure = False
        self.dataset["tasks"] = DataSetSection()
        self._tasks = []
        self._node = node
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._arg_hash_counts: dict[str, int] = {}
        self._primary_ready: dict[str, asyncio.Event] = {}
        self._primary_api_errors: dict[str, RunnerAPIError] = {}


    async def recover_orphaned_datasets(self):
        previous_node_name = self._kwargs.get("previous_node_name", None)
        previous_task_id = self._kwargs.get("previous_task_id", None)
        if previous_node_name and previous_task_id:
            previous_dataset_field_name = f"{previous_node_name}.{previous_task_id}"
            datasets = await context.server.values.query_records(DataSet, "by_field_name", field_name=previous_dataset_field_name)
            existing_dataset = datasets[0] if datasets else None
            if existing_dataset:
                self._existing_dataset = existing_dataset
                self.info(f"Found existing dataset: {previous_dataset_field_name}")
                return

    def _arg_row(self, args: List[Model]) -> dict[str, Model]:
        import inspect

        sig = inspect.signature(self._node)
        param_names = list(sig.parameters.keys())
        task_dict = {}
        for i, m in enumerate(args):
            if i < len(param_names):
                task_dict[f"arg_{param_names[i]}"] = m
            else:
                task_dict[f"arg_{i}"] = m
        return task_dict

    async def _run_node(
        self,
        args: List[Model],
        combined_arg_hash: Optional[str] = None,
        row_name: Optional[str] = None,
        is_primary: bool = True,
    ):
        if combined_arg_hash is None:
            combined_arg_hash = compute_arg_hash(args)
        if row_name is None:
            row_name = combined_arg_hash

        if not is_primary:
            ready = self._primary_ready.get(combined_arg_hash)
            if ready is None:
                raise ValueError(
                    f"Duplicate arg_hash {combined_arg_hash} has no primary task"
                )
            self.info(
                f"Duplicate arg_hash {combined_arg_hash}; reusing the first result as row {row_name}"
            )
            await ready.wait()
            api_error = self._primary_api_errors.get(combined_arg_hash)
            if api_error is not None:
                raise api_error
            if combined_arg_hash not in self.dataset["tasks"]:
                raise ValueError(
                    f"Duplicate arg_hash {combined_arg_hash} has no primary result row"
                )
            primary_row = self.dataset["tasks"].get_item(combined_arg_hash)
            row = {
                key: model
                for key, model in primary_row.items()
                if not key.startswith("arg_")
            }
            row.update(self._arg_row(args))
            self.dataset["tasks"].add_row(row, name=row_name)
            return None

        try:
            await self.recover_orphaned_datasets()

            if self._existing_dataset and combined_arg_hash in self._existing_dataset["tasks"].data:
                row = self._existing_dataset["tasks"].get_item(combined_arg_hash)
                success = row.get("success", None)
                if success is not None:
                    success = success.value
                if success:
                    self.info(f"Skipping node with arg_hash: {combined_arg_hash}")
                    self.dataset["tasks"].add_row(row, row_name)
                    return None

            self.info(f"Running node with arg_hash: {combined_arg_hash}")

            task_dict = self._arg_row(args)

            try:
                if self._semaphore:
                    async with self._semaphore:
                        if asyncio.iscoroutinefunction(self._node):
                            result = await self._node(*args, **self._kwargs)
                        else:
                            result = self._node(*args, **self._kwargs)
                else:
                    if asyncio.iscoroutinefunction(self._node):
                        result = await self._node(*args, **self._kwargs)
                    else:
                        result = self._node(*args, **self._kwargs)
            except RunnerAPIError:
                raise
            except Exception as e:
                self._failure = True
                self.error(f"Error running node: {e}")
                task_dict["success"] = BooleanData(value=False)
                task_dict["error"] = StringData(field_name="error_message",value=str(e))
                self.dataset["tasks"].add_row(task_dict, name=row_name)
                return None

            if result is None:
                task_dict["success"] = BooleanData(value=False)
            elif isinstance(result, bool):
                task_dict["success"] = BooleanData(value=result)
            elif isinstance(result, (SimstackResult, Model)):
                references, models = await process_result_helper(result, self.task_id)
                if isinstance(result, SimstackResult):
                    if result.status != TaskStatus.COMPLETED:
                        self._failure = True
                    task_dict["success"] = BooleanData(value=result.status == TaskStatus.COMPLETED)
                else:
                    task_dict["success"] = BooleanData(value=True)
                if references:
                    for reference, model in zip(references, models):
                        task_dict[f"result_{reference.variable_name}"] = model
            elif isinstance(result, (list, tuple)) and all(isinstance(m, Model) for m in result):
                task_dict["success"] = BooleanData(value=True)
                for i, m in enumerate(result):
                    task_dict[f"result_{i}"] = m

            self.dataset["tasks"].add_row(task_dict, name=row_name)
            return result
        except RunnerAPIError as exc:
            self._failure = True
            self._primary_api_errors[combined_arg_hash] = exc
            raise
        finally:
            ready = self._primary_ready.get(combined_arg_hash)
            if ready is not None and not ready.is_set():
                ready.set()

    def create_tasks(self, *args: Model):
        args_list = list(args)
        # Match Node identity, including materialized dataset/list members and
        # immutable file contents, before coalescing duplicate calculations.
        combined_arg_hash = compute_arg_hash(args_list)
        seen = self._arg_hash_counts.get(combined_arg_hash, 0)
        self._arg_hash_counts[combined_arg_hash] = seen + 1
        if seen == 0:
            row_name = combined_arg_hash
            is_primary = True
            self._primary_ready[combined_arg_hash] = asyncio.Event()
        else:
            row_name = f"{combined_arg_hash}#{seen}"
            is_primary = False

        task = asyncio.create_task(
            self._run_node(args_list, combined_arg_hash, row_name, is_primary)
        )
        self._tasks.append(task)
        return task

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        errors = [exc_val] if exc_val is not None else []
        if self._tasks:
            # Cancelling a remote waiter can terminate its job. Drain siblings
            # before propagating an infrastructure failure instead.
            outcomes = await asyncio.gather(*self._tasks, return_exceptions=True)
            errors.extend(result for result in outcomes if isinstance(result, BaseException))
        if errors:
            self._failure = True
            if exc_val is not None and not isinstance(exc_val, Exception):
                return False
            for error in errors:
                if isinstance(error, RunnerAPIError):
                    raise error
            if exc_val is not None:
                return False
            raise errors[0]
        
        await context.server.values.publish_value(self.dataset)
        self.info("MassRunner saving dataset")

        #
        # # Check if any task failed
        # for idx in range(len(self.dataset['tasks'].data)):
        #     item = await self.dataset['tasks'].get_item(idx)
        #     if 'success' in item and hasattr(item['success'], 'value') and not item['success'].value:
        #         return self.fail("One or more tasks failed")

        return self.succeed()
