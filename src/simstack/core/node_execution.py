"""Execute the scientific function inside its task working directory."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Union

from simstack.core.artifacts import ArtifactArguments, create_artifacts
from simstack.core.context import GlobalState
from simstack.core.definitions import TaskStatus
from simstack.core.node_runner import NodeRunner
from simstack.core.simstack_result import SimstackResult
from simstack.core.task_id import clear_task_id, set_task_id
from simstack.models.document import Model
from simstack.util.sanitized_output import sanitized_tail

if TYPE_CHECKING:
    from simstack.core.node import Node

logger = logging.getLogger("Node")


async def execute_node_locally(node: Node, context: GlobalState, *, process_id: int | None = None) -> Union[Model, SimstackResult, None]:
    """Run the scientific function, publish its outputs and restore the caller's context."""
    assert node.registry_entry is not None
    node.registry_entry.started_at = datetime.now()
    await node.set_status(TaskStatus.RUNNING, process_id=process_id)
    logger.info(
        f"Task task_id: {node.id} is started on {node.parameters.resource} in Node:execute_node_locally"
    )
    previous_node_name = context.current_node_name
    context.current_node_name = node.name
    original_dir = Path.cwd()
    try:
        node_runner = NodeRunner(node._func.__name__, node.id)
        node_kwargs = {
            "node_runner": node_runner,
            "parent_id": node.id,
            "task_id": node.id,
            "call_path": node.call_path,
            "parent_parameters": node.parameters,  # this must have a name different from parameters, because
            # otherwise this setting will override all the parameters of
            # the child nodes. Server resolves inherited allocation.
            "recompute_artifacts": node.recompute_artifacts,
            "custom_name": node.custom_name,
            "arg_hash": node._arg_hash,
            "function_hash": node._function_hash,
        }

        if node.parameters.force_rerun:
            node_kwargs["force_rerun"] = True

        path = Path(context.config.workdir) / node.name / str(node.id)
        # Create the directory if it doesn't exist
        path.mkdir(parents=True, exist_ok=True)
        os.chdir(path)
        logger.debug(
            f"Task task_id: {node.id} successfully changed to directory: {path.absolute()}"
        )

        # real_func = getattr(real_func_wrapper, '_inner', real_func_wrapper)
        real_func = node._func
        result = None
        set_task_id(node.registry_entry.id)
        try:
            if node.is_async:
                result = await real_func(*node._args, **node_kwargs)
            else:
                result = real_func(*node._args, **node_kwargs)
            if isinstance(result, bool):
                node._execution_return_kind = "bool"
            elif isinstance(result, SimstackResult):
                node._execution_return_kind = "multiple"
            elif isinstance(result, Model):
                node._execution_return_kind = "model"
            else:
                node._execution_return_kind = "none"
            node.registry_entry.return_kind = node._execution_return_kind
        except Exception as e:
            # Preserve the scientific error before publishing diagnostics.
            clean_error = sanitized_tail(
                str(e),
                None,
                limit=None,
            )
            if node.registry_entry:
                node.registry_entry.error = clean_error
            logger.error(
                "Task task_id: %s node function error for node: %s msg: %s",
                node.id,
                node.name,
                clean_error,
            )
            # Publish available diagnostics, in particular info_files.
            try:
                await node.process_results(node_runner)
            except Exception:
                logger.warning("Task %s diagnostic files could not be published", node.id, exc_info=True)
            raise
        finally:
            clear_task_id()

        node.registry_entry.completed_at = datetime.now()

        new_task_status, result = await node.process_results(result)
        node._execution_return_kind = node.registry_entry.return_kind

        if new_task_status == TaskStatus.COMPLETED:
            artifact_arguments = ArtifactArguments(result, node.id)
            artifact_arguments.add_attributes(
                node._func, *node._args, **node_kwargs
            )
            node.registry_entry.artifact_ids = await create_artifacts(
                artifact_arguments, node.registry_entry
            )
        await node.set_status(
            new_task_status
        )  # this will also commit the registry entry

        logger.info(
            f"Task task_id: {node.id} is finished on resource: {node.parameters.resource} with task status: {new_task_status}"
        )
        if new_task_status != TaskStatus.COMPLETED:
            return None
        return result
    except Exception as exc:
        if not node.registry_entry.error:
            node.registry_entry.error = sanitized_tail(str(exc), None, limit=None)
        try:
            await node.set_status(TaskStatus.FAILED)
        except Exception:
            logger.warning("Task %s failure awaits reconciliation", node.id, exc_info=True)
        raise
    finally:
        context.current_node_name = previous_node_name
        os.chdir(original_dir)
        logger.debug(
            f"Task task_id: {node.id} successfully back to directory: {original_dir.absolute()}"
        )
