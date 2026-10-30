"""Versioned server route templates and safe client path builders."""

from enum import StrEnum
from urllib.parse import quote, urlencode

from .constants import FILE_TRANSFER_API_PREFIX
from .errors import RunnerProtocolError


class RunnerRoute(StrEnum):
    BOOTSTRAP = "/bootstrap"
    TASK_BOOTSTRAP = "/tasks/{task_id}/bootstrap"
    TASK_CLAIM = "/tasks/claim"
    ACTIVE_TASKS = "/tasks/active"
    TASK = "/tasks/{task_id}"
    CHILD_RESOLVE = "/tasks/{parent_id}/children/resolve"
    CHILD = "/tasks/{parent_id}/children/{child_id}"
    TASK_LEASE = "/tasks/{task_id}/lease"
    TASK_STATUS = "/tasks/{task_id}/status"
    TASK_COMMIT = "/tasks/{task_id}/commit"
    TASK_CACHE = "/tasks/{task_id}/cache/resolve"
    TASK_FAIL = "/tasks/{task_id}/fail"
    TASK_FILE_CONTENT = "/tasks/{task_id}/files/{file_stack_id}/content"
    TASK_PROGRAM_POLICY = "/tasks/{task_id}/program-policy"
    TASK_PROGRAM_PLAN = "/tasks/{task_id}/programs/{program}"
    DATASET_SNAPSHOT = "/tasks/{task_id}/datasets/{dataset_id}/sections/{section_key:path}/snapshot"
    DATASET_ROWS = "/tasks/{task_id}/datasets/{dataset_id}/sections/{section_key:path}/rows"
    HEARTBEAT = "/heartbeat"
    LOG_BATCH = "/logs/batch"
    WORKFLOW_PENDING = "/workflow-installations/pending"
    WORKFLOW_CLAIM = "/workflow-installations/{installation_id}/claim"
    WORKFLOW_ARCHIVE = "/workflow-installations/{installation_id}/archive"
    WORKFLOW_STATUS = "/workflow-installations/{installation_id}/status"


def workflow_installation_path(installation_id: str, action: str) -> str:
    routes = {"claim": RunnerRoute.WORKFLOW_CLAIM, "archive": RunnerRoute.WORKFLOW_ARCHIVE, "status": RunnerRoute.WORKFLOW_STATUS}
    if action not in routes:
        raise RunnerProtocolError("Unknown workflow installation action")
    return routes[action].format(installation_id=_path_segment(installation_id, field="installation_id"))


def task_file_content_path(task_id: str, file_stack_id: str, *, claim_id: str, attempt: int) -> str:
    path = RunnerRoute.TASK_FILE_CONTENT.format(
        task_id=_path_segment(task_id, field="task_id"),
        file_stack_id=_path_segment(file_stack_id, field="file_stack_id"),
    )
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}"


def workflow_archive_path(installation_id: str, *, claim_id: str, attempt: int, task_id: str | None = None) -> str:
    path = workflow_installation_path(installation_id, "archive")
    query = _claim_query(claim_id=claim_id, attempt=attempt)
    if task_id is not None:
        query += "&" + urlencode({"task_id": _canonical_query_value(task_id, field="task_id")})
    return f"{path}?{query}"


class FileTransferRoute(StrEnum):
    COLLECTION = FILE_TRANSFER_API_PREFIX
    ITEM = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}"
    UPLOAD = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}/upload"
    DOWNLOAD = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}/download"
    COMPLETE = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}/complete"
    FAIL = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}/fail"
    RESTART = f"{FILE_TRANSFER_API_PREFIX}/{{transfer_id}}/restart"


BOOTSTRAP_ROUTE = RunnerRoute.BOOTSTRAP
TASK_BOOTSTRAP_ROUTE = RunnerRoute.TASK_BOOTSTRAP
TASK_CLAIM_ROUTE = RunnerRoute.TASK_CLAIM
ACTIVE_TASKS_ROUTE = RunnerRoute.ACTIVE_TASKS
TASK_ROUTE = RunnerRoute.TASK
CHILD_RESOLVE_ROUTE = RunnerRoute.CHILD_RESOLVE
CHILD_ROUTE = RunnerRoute.CHILD
TASK_LEASE_ROUTE = RunnerRoute.TASK_LEASE
TASK_STATUS_ROUTE = RunnerRoute.TASK_STATUS
TASK_COMMIT_ROUTE = RunnerRoute.TASK_COMMIT
TASK_CACHE_ROUTE = RunnerRoute.TASK_CACHE
TASK_FAIL_ROUTE = RunnerRoute.TASK_FAIL
TASK_FILE_CONTENT_ROUTE = RunnerRoute.TASK_FILE_CONTENT
TASK_PROGRAM_POLICY_ROUTE = RunnerRoute.TASK_PROGRAM_POLICY
TASK_PROGRAM_PLAN_ROUTE = RunnerRoute.TASK_PROGRAM_PLAN
DATASET_SNAPSHOT_ROUTE = RunnerRoute.DATASET_SNAPSHOT
DATASET_ROWS_ROUTE = RunnerRoute.DATASET_ROWS


def dataset_section_path(task_id: str, dataset_id: str, section_key: str, *, action: str,
                         claim_id: str, attempt: int, kind: str, **query) -> str:
    if action not in {"snapshot", "rows"} or kind not in {"dict", "tuple"}:
        raise RunnerProtocolError("Invalid dataset section operation")
    route = RunnerRoute.DATASET_SNAPSHOT if action == "snapshot" else RunnerRoute.DATASET_ROWS
    path = str(route).replace("{section_key:path}", "{section_key}").format(task_id=_path_segment(task_id, field="task_id"),
                        dataset_id=_path_segment(dataset_id, field="dataset_id"),
                        section_key=_path_segment(section_key, field="section_key"))
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}&{urlencode({'kind': kind, **query})}"
HEARTBEAT_ROUTE = RunnerRoute.HEARTBEAT
LOG_BATCH_ROUTE = RunnerRoute.LOG_BATCH
FILE_TRANSFER_COLLECTION_ROUTE = FileTransferRoute.COLLECTION
FILE_TRANSFER_ITEM_ROUTE = FileTransferRoute.ITEM
FILE_TRANSFER_UPLOAD_ROUTE = FileTransferRoute.UPLOAD
FILE_TRANSFER_DOWNLOAD_ROUTE = FileTransferRoute.DOWNLOAD
FILE_TRANSFER_COMPLETE_ROUTE = FileTransferRoute.COMPLETE
FILE_TRANSFER_FAIL_ROUTE = FileTransferRoute.FAIL
FILE_TRANSFER_RESTART_ROUTE = FileTransferRoute.RESTART


def _path_segment(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RunnerProtocolError(f"Runner path requires canonical {field}")
    return quote(value, safe="")


def _claim_query(*, claim_id: str, attempt: int) -> str:
    if type(attempt) is not int or attempt < 1:
        raise RunnerProtocolError("Runner path attempt must be an integer >= 1")
    return urlencode(
        {
            "claim_id": _canonical_query_value(claim_id, field="claim_id"),
            "attempt": attempt,
        }
    )


def _canonical_query_value(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RunnerProtocolError(f"Runner query requires canonical {field}")
    return value


def task_bootstrap_path(task_id: str, *, claim_id: str, attempt: int) -> str:
    path = TASK_BOOTSTRAP_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}"


def task_path(
    task_id: str,
    *,
    claim_id: str | None = None,
    attempt: int | None = None,
) -> str:
    path = TASK_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))
    if claim_id is None and attempt is None:
        return path
    if claim_id is None or attempt is None:
        raise RunnerProtocolError("Runner task path requires claim_id and attempt")
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}"


def child_resolve_path(parent_id: str) -> str:
    return CHILD_RESOLVE_ROUTE.format(
        parent_id=_path_segment(parent_id, field="parent_id")
    )


def child_path(
    parent_id: str,
    child_id: str,
    *,
    claim_id: str,
    attempt: int,
) -> str:
    path = CHILD_ROUTE.format(
        parent_id=_path_segment(parent_id, field="parent_id"),
        child_id=_path_segment(child_id, field="child_id"),
    )
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}"


def task_lease_path(task_id: str) -> str:
    return TASK_LEASE_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))


def task_status_path(task_id: str) -> str:
    return TASK_STATUS_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))


def task_commit_path(task_id: str) -> str:
    return TASK_COMMIT_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))


def task_cache_path(task_id: str) -> str:
    return TASK_CACHE_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))


def task_fail_path(task_id: str) -> str:
    return TASK_FAIL_ROUTE.format(task_id=_path_segment(task_id, field="task_id"))


def active_tasks_path(statuses: tuple[str, ...] | list[str]) -> str:
    values = [_canonical_query_value(status, field="status") for status in statuses]
    return (
        f"{ACTIVE_TASKS_ROUTE}?{urlencode([('statuses', value) for value in values])}"
    )


def file_transfer_path(transfer_id: str) -> str:
    return FILE_TRANSFER_ITEM_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_upload_path(transfer_id: str) -> str:
    return FILE_TRANSFER_UPLOAD_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_download_path(transfer_id: str) -> str:
    return FILE_TRANSFER_DOWNLOAD_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_complete_path(transfer_id: str) -> str:
    return FILE_TRANSFER_COMPLETE_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_fail_path(transfer_id: str) -> str:
    return FILE_TRANSFER_FAIL_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_restart_path(transfer_id: str) -> str:
    return FILE_TRANSFER_RESTART_ROUTE.format(
        transfer_id=_path_segment(transfer_id, field="transfer_id")
    )


def file_transfer_list_path(
    *,
    role: str,
    status: str | None,
    limit: int,
) -> str:
    role_value = _canonical_query_value(role, field="role")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RunnerProtocolError("File transfer list limit must be between 1 and 100")
    query: list[tuple[str, str | int]] = [("role", role_value), ("limit", limit)]
    if status is not None:
        query.append(("status", _canonical_query_value(status, field="status")))
    return f"{FILE_TRANSFER_COLLECTION_ROUTE}?{urlencode(query)}"


def task_program_policy_path(task_id: str) -> str:
    return RunnerRoute.TASK_PROGRAM_POLICY.format(task_id=_path_segment(task_id, field="task_id"))

def task_program_plan_path(task_id: str, program: str, *, claim_id: str, attempt: int) -> str:
    path = RunnerRoute.TASK_PROGRAM_PLAN.format(task_id=_path_segment(task_id, field="task_id"), program=_path_segment(program, field="program"))
    return f"{path}?{_claim_query(claim_id=claim_id, attempt=attempt)}"
