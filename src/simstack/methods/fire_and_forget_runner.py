import asyncio
import inspect
from typing import List, Callable, Any, Optional

from odmantic import Model

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.core.node_runner import NodeRunner
from simstack.core.process_results import process_result_helper
from simstack.models import FireAndForgetResult, NamedDataReference

class FireAndForgetRunner(NodeRunner):
    def __init__(self, node: Callable[..., Any], max_concurrency: Optional[int] = None, **kwargs):
        # We need to handle the case where "node_runner" might be in kwargs, just like MassRunner does.
        # However, NodeRunner.__init__ expects (name, task_id, logger).
        # MassRunner does: super().__init__(kwargs["node_runner"].name, kwargs["node_runner"].task_id, kwargs["node_runner"].logger )
        
        node_runner = kwargs.get("node_runner")
        if node_runner:
            super().__init__(node_runner.name, node_runner.task_id, node_runner.logger)
            self.call_path = getattr(node_runner, "call_path", "NA")
        else:
            # Fallback if not provided, though it's expected in this context
            super().__init__(kwargs.get("name", "FireAndForgetRunner"), kwargs.get("task_id", "NA"), kwargs.get("logger"))
            self.call_path = "NA"

        self._node = node
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._tasks = []
        self._kwargs = kwargs.copy()

    async def _run_node(self, args: List[Model]):
        sig = inspect.signature(self._node)
        param_names = list(sig.parameters.keys())

        node_name = getattr(self._node, "__name__", "unknown_node")
        full_call_path = f"{self.call_path}/{node_name}"

        input_models_dict = {}
        for i, m in enumerate(args):
            arg_name = f"arg_{param_names[i]}" if i < len(param_names) else f"arg_{i}"
            input_models_dict[arg_name] = NamedDataReference.from_variable(m, variable_name=arg_name, task_id=self.task_id)

        result_models_dict = {}
        success = False

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
            
            if result is None:
                success = False
            elif isinstance(result, bool):
                success = result
            elif isinstance(result, (SimstackResult, Model)):
                references, models = await process_result_helper(result, self.task_id)
                if isinstance(result, SimstackResult):
                    success = (result.status == TaskStatus.COMPLETED)
                else:
                    success = True
                
                if references:
                    for reference, model in zip(references, models):
                        result_models_dict[reference.variable_name] = reference
            elif isinstance(result, (list, tuple)) and all(isinstance(m, Model) for m in result):
                success = True
                for i, m in enumerate(result):
                    res_name = f"result_{i}"
                    result_models_dict[res_name] = NamedDataReference.from_variable(m, variable_name=res_name, task_id=self.task_id)
            else:
                # Other types of results
                success = True

        except Exception as e:
            self.error(f"Error running node in FireAndForgetRunner: {e}")
            success = False

        # Immediately write to DB
        ff_result = FireAndForgetResult(
            call_path=full_call_path,
            input_models=input_models_dict,
            result_models=result_models_dict,
            success=success
        )
        
        db = context.db
        await db.save(ff_result)
        return ff_result

    def create_tasks(self, *args: Model):
        task = asyncio.create_task(self._run_node(list(args)))
        self._tasks.append(task)
        return task

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        return self.succeed()
