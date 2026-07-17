import asyncio
from typing import List, Callable, Any, Optional

from odmantic import Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.hash import complex_hash_function
from simstack.core.simstack_result import SimstackResult
from simstack.core.node_runner import NodeRunner
from simstack.core.process_results import process_result_helper
from simstack.models import DataSetMetadata, DataSet, DataSetSection, BooleanData, StringData

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


    async def recover_orphaned_datasets(self):
        previous_node_name = self._kwargs.get("previous_node_name", None)
        previous_task_id = self._kwargs.get("previous_task_id", None)
        if previous_node_name and previous_task_id:
            db = context.db
            previous_dataset_field_name = f"{previous_node_name}.{previous_task_id}"
            existing_dataset = await db.find_one(DataSet, DataSet.field_name == previous_dataset_field_name)
            if existing_dataset:
                self._existing_dataset = existing_dataset
                self.info(f"Found existing dataset: {previous_dataset_field_name}")
                return

    async def _run_node(self, args: List[Model]):
        await self.recover_orphaned_datasets()

        import inspect
        sig = inspect.signature(self._node)
        param_names = list(sig.parameters.keys())

        arg_hashes = [complex_hash_function(arg) for arg in args]
        combined_arg_hash = complex_hash_function(arg_hashes)
        
        if self._existing_dataset and combined_arg_hash in self._existing_dataset["tasks"].data:
            row = self._existing_dataset["tasks"].get_item(combined_arg_hash)
            success = row.get("success", None)
            if success is not None:
                success = success.value
            if success:
                self.info(f"Skipping node with arg_hash: {combined_arg_hash}")
                self.dataset["tasks"].add_row(row, combined_arg_hash)
                return None
        
        self.info(f"Running node with arg_hash: {combined_arg_hash}")

        task_dict = {}

        for i, m in enumerate(args):
            if i < len(param_names):
                task_dict[f"arg_{param_names[i]}"] = m
            else:
                task_dict[f"arg_{i}"] = m

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
        except Exception as e:
            self._failure = True
            self.error(f"Error running node: {e}")
            task_dict["success"] = BooleanData(value=False)
            task_dict["error"] = StringData(field_name="error_message",value=str(e))
            self.dataset["tasks"].add_row(task_dict, name=combined_arg_hash)
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
        
        self.dataset["tasks"].add_row(task_dict, name=combined_arg_hash)
        return result

    def create_tasks(self, *args: Model):

        task = asyncio.create_task(self._run_node(list(args)))
        self._tasks.append(task)
        return task

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._tasks:
            await asyncio.gather(*self._tasks)
        
        db = context.db
        await self.dataset.save(db)
        self.info("MassRunner saving dataset")

        #
        # # Check if any task failed
        # for idx in range(len(self.dataset['tasks'].data)):
        #     item = await self.dataset['tasks'].get_item(idx)
        #     if 'success' in item and hasattr(item['success'], 'value') and not item['success'].value:
        #         return self.fail("One or more tasks failed")

        return self.succeed()
