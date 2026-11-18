"""Publish and restore scientific outputs belonging to a node invocation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Union

from simstack.core.context import GlobalState
from simstack.core.definitions import TaskStatus
from simstack.core.process_results import process_result_helper
from simstack.core.simstack_result import SimstackResult
from simstack.models.document import Model
from simstack.models.file_list import FileList
from simstack.models.files import FileStack
from simstack.models.simstack_model import is_simstack_model
from simstack.util.sanitized_output import sanitized_tail

if TYPE_CHECKING:
    from simstack.core.node import Node

logger = logging.getLogger("Node")


async def load_results(
    node: Node, context: GlobalState, return_kind: str | None = None
) -> Union[Model, SimstackResult, bool, None]:
    """
    Loads the results associated with a specific task through the Server API. This
    method verifies whether the task has valid result identifiers. If valid
    identifiers (results_id and results_table_name) exist, it attempts to fetch
    the outputs.

    If the task status is not TaskStatus.COMPLETED None is returned which results in a RuntimeError
    If the results are not found or if the retrieval process fails, a `ValueError` is raised.

    :raises ValueError: If the task has completed but lacks output identifiers
        (`results_id` or `results_table_name`), or if there is any error
        during the process of loading the results.

    :return: The retrieved scientific task outputs.
    """
    assert node.registry_entry is not None
    logger.info(
        f"Task task_id: {node.id} loading results with task status {node.status}"
    )
    try:
        if node.registry_entry.status != TaskStatus.COMPLETED:
            return None
        persisted_return_kind = return_kind or getattr(
            node.registry_entry, "return_kind", None
        )
        if persisted_return_kind == "none":
            return None
        if persisted_return_kind == "bool":
            return True
        if not node.registry_entry.results_references:
            if persisted_return_kind == "multiple":
                return SimstackResult(
                    status=node.registry_entry.status,
                    custom_name=node.registry_entry.custom_name,
                    message=node.registry_entry.message,
                    error_message=node.registry_entry.error,
                    info_files=list(node.registry_entry.info_files),
                )
            return True

        simstack_result = SimstackResult(
            status=node.registry_entry.status,
            custom_name=node.registry_entry.custom_name,
            message=node.registry_entry.message,
            error_message=node.registry_entry.error,
            info_files=list(node.registry_entry.info_files),
        )
        result = None
        values = await context.server.values.resolve_task_outputs(node.registry_entry)
        if len(values) != len(node.registry_entry.results_references):
            raise ValueError("Task output snapshot does not match its result references")
        for ref, result in zip(node.registry_entry.results_references, values):
            if result is None:
                logger.error(
                    f"Task task_id: {node.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                )
                raise ValueError(
                    f"Task task_id: {node.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                )
            simstack_result.__setattr__(ref.variable_name, result)

        logger.info(f"Task task_id: {node.id} loaded outputs")

        if (
            len(node.registry_entry.results_references) == 1
            and persisted_return_kind != "multiple"
        ):
            return result  # there is only one result, return it directly
        else:
            return simstack_result  # return the SimstackResult with all results

    except Exception as exc:
        # A read failure does not invalidate a completed scientific task.
        raise ValueError(f"Task {node.id} failed to load outputs: {exc}") from exc


async def publish_results(node: Node, context: GlobalState, result: Any) -> tuple[TaskStatus, Any]:
    assert node.registry_entry is not None
    # each of the following if sets the result either to a valid value or None
    new_task_status = TaskStatus.COMPLETED
    if result is None:
        node.registry_entry.return_kind = "none"
        logger.warning(f"Task task_id: {node.id} returned None")
        new_task_status = TaskStatus.FAILED  # result is None
    elif isinstance(result, bool):
        node.registry_entry.return_kind = "bool"
        if not result:
            node.registry_entry.error = "Node returned False"
            new_task_status = TaskStatus.FAILED
            result = None
    elif is_simstack_model(result) or isinstance(result, SimstackResult):
        if isinstance(result, SimstackResult):
            node.registry_entry.return_kind = "multiple"
            new_task_status = result.status
            if result.custom_name is not None:
                node.custom_name = result.custom_name
                node.registry_entry.custom_name = result.custom_name
                object.__setattr__(node.registry_entry, "_outcome_custom_name", result.custom_name)

            for file_stack in result.info_files:
                if file_stack:
                    if isinstance(file_stack, FileStack):
                        logger.info(
                            f"Task task_id: {node.id} saving info file: {file_stack.name} {file_stack.id}"
                        )
                        if node.registry_entry.info_files is None:
                            node.registry_entry.info_files = FileList()
                        await context.server.values.publish_value(file_stack)
                        node.registry_entry.info_files.append(file_stack)
                    else:
                        logger.error(
                            f"Task task_id: {node.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                        )
                else:
                    logger.error(f"Task task_id: {node.id} saving info-file is NONE")
                    raise ValueError("saving info file is NONE")

            if result.error_message is not None and result.error_message != "":
                clean_error = sanitized_tail(
                    result.error_message,
                    None,
                    limit=None,
                )
                node.registry_entry.error = clean_error
                logger.error(
                    "Task task_id: %s returned with error: %s",
                    node.id,
                    clean_error,
                )
            if result.message is not None and result.message != "":
                node.registry_entry.message = result.message
                logger.info(f"Task task_id: {node.id} message: {result.message}")
        else:
            node.registry_entry.return_kind = "model"
            if hasattr(result, "status"):
                new_task_status = result.status
            elif hasattr(result, "task_status"):
                new_task_status = result.task_status

        results_references, result_models = await process_result_helper(result, str(node.id))
        publication = None
        if result_models:
            result_models, publication = await context.server.values.publish_task_outputs(
                result_models, execution=context.server.tasks.execution(node.registry_entry),
                repository_revision=node.registry_entry.repository_revision)
        object.__setattr__(node.registry_entry, "_output_publication", publication)
        node.registry_entry.results_references = results_references
        node.registry_entry.status = new_task_status

        if isinstance(result, SimstackResult):
            for reference, value in zip(results_references, result_models):
                # The immediate SimstackResult.files API remains a list;
                # its stored result reference is the FileListModel wrapper.
                setattr(result, reference.variable_name,
                        list(value) if reference.variable_name == "files" else value)
        elif len(results_references) == 1:
            result = result_models[0]
            node.registry_entry.return_kind = "model"

    else:
        node.registry_entry.return_kind = "none"
        logger.warning(
            f"Task task_id: {node.id} returned a result of type {type(result)} which is not a SimstackModel or a SimstackResult"
        )
        new_task_status = TaskStatus.FAILED
    return new_task_status, result
