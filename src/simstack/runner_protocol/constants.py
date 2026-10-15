"""Single source of versioned runner protocol literals."""

from enum import StrEnum

RUNNER_API_VERSION = "1"
RUNNER_TASK_SCHEMA_VERSION = 1
API_ROOT = "/api"
RUNNER_API_PREFIX = f"{API_ROOT}/runner"
FILE_TRANSFER_API_PREFIX = f"{API_ROOT}/file-transfers"
RUNNER_CURRENT_RESOURCE_ALIAS = "self"

DEFAULT_RUNNER_LEASE_SECONDS = 120
MIN_RUNNER_LEASE_SECONDS = 30
MAX_RUNNER_LEASE_SECONDS = 3600


class RunnerScope(StrEnum):
    BOOTSTRAP = "runner:bootstrap"
    HEARTBEAT = "runner:heartbeat"
    TASK_CLAIM = "tasks:claim"
    TASK_READ = "tasks:read"
    TASK_LEASE = "tasks:lease"
    TASK_STATUS = "tasks:status"
    TASK_COMMIT = "tasks:commit"
    TASK_FAIL = "tasks:fail"
    TASK_CHILDREN = "tasks:children"
    LOG_WRITE = "logs:write"
    FILE_CREATE = "file:create"
    FILE_UPLOAD = "file:upload"
    FILE_DOWNLOAD = "file:download"
    FILE_STATUS = "file:status"
    FILE_COMPLETE = "file:complete"


RUNNER_BOOTSTRAP_SCOPE = RunnerScope.BOOTSTRAP
RUNNER_HEARTBEAT_SCOPE = RunnerScope.HEARTBEAT
TASK_CLAIM_SCOPE = RunnerScope.TASK_CLAIM
TASK_READ_SCOPE = RunnerScope.TASK_READ
TASK_LEASE_SCOPE = RunnerScope.TASK_LEASE
TASK_STATUS_SCOPE = RunnerScope.TASK_STATUS
TASK_COMMIT_SCOPE = RunnerScope.TASK_COMMIT
TASK_FAIL_SCOPE = RunnerScope.TASK_FAIL
TASK_CHILDREN_SCOPE = RunnerScope.TASK_CHILDREN
LOG_WRITE_SCOPE = RunnerScope.LOG_WRITE
FILE_CREATE_SCOPE = RunnerScope.FILE_CREATE
FILE_UPLOAD_SCOPE = RunnerScope.FILE_UPLOAD
FILE_DOWNLOAD_SCOPE = RunnerScope.FILE_DOWNLOAD
FILE_STATUS_SCOPE = RunnerScope.FILE_STATUS
FILE_COMPLETE_SCOPE = RunnerScope.FILE_COMPLETE

RUNNER_SUPERVISOR_SCOPES = (
    RUNNER_BOOTSTRAP_SCOPE,
    RUNNER_HEARTBEAT_SCOPE,
    TASK_CLAIM_SCOPE,
    TASK_READ_SCOPE,
    LOG_WRITE_SCOPE,
    FILE_UPLOAD_SCOPE,
    FILE_STATUS_SCOPE,
)
RUNNER_EXECUTION_SCOPES = (
    TASK_READ_SCOPE,
    TASK_LEASE_SCOPE,
    TASK_STATUS_SCOPE,
    TASK_COMMIT_SCOPE,
    TASK_FAIL_SCOPE,
    TASK_CHILDREN_SCOPE,
    LOG_WRITE_SCOPE,
    FILE_CREATE_SCOPE,
    FILE_DOWNLOAD_SCOPE,
    FILE_STATUS_SCOPE,
    FILE_COMPLETE_SCOPE,
)
KNOWN_RUNNER_SCOPES = frozenset(RunnerScope)


class RunnerTaskStatus(StrEnum):
    SUBMITTED = "submitted"
    RETRIEVED = "retrieved"
    RUNNING = "running"
    SLURM_QUEUED = "slurm_queued"
    SLURM_RUNNING = "slurm_running"
    FAILED = "failed"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    RECOVERED = "recovered"


class RunnerEventType(StrEnum):
    SHUTDOWN = "shutdown"
    RUNNER_STARTED = "runner_started"
    NODE_STARTED = "node_started"
    NODE_SUBMIT = "node_submit"
    ALIVE = "alive"
    RESTART = "restart"
    CRONTAB_OK = "crontab_ok"
    CRONTAB_GONE = "crontab_gone"


class RunnerProcessType(StrEnum):
    NODE_RUNNER = "node_runner"
    RESOURCE_RUNNER = "resource_runner"


class RunnerQueue(StrEnum):
    DEFAULT = "default"
    SLURM_QUEUE = "slurm-queue"


ACTIVE_RUNNER_TASK_STATUSES = frozenset(
    {
        RunnerTaskStatus.RETRIEVED,
        RunnerTaskStatus.RUNNING,
        RunnerTaskStatus.SLURM_QUEUED,
        RunnerTaskStatus.SLURM_RUNNING,
    }
)
RUNNER_CHILD_PENDING_STATUSES = frozenset(
    {RunnerTaskStatus.SUBMITTED, *ACTIVE_RUNNER_TASK_STATUSES}
)
RUNNER_TERMINAL_SUCCESS_STATUS = RunnerTaskStatus.COMPLETED
RUNNER_STATUS_TRANSITIONS = {
    RunnerTaskStatus.RETRIEVED: frozenset(
        {
            RunnerTaskStatus.RETRIEVED,
            RunnerTaskStatus.RUNNING,
            RunnerTaskStatus.SLURM_QUEUED,
        }
    ),
    RunnerTaskStatus.RUNNING: frozenset(
        {
            RunnerTaskStatus.RUNNING,
            RunnerTaskStatus.SLURM_QUEUED,
            RunnerTaskStatus.SLURM_RUNNING,
            RunnerTaskStatus.TIMEOUT,
        }
    ),
    RunnerTaskStatus.SLURM_QUEUED: frozenset(
        {
            RunnerTaskStatus.SLURM_QUEUED,
            RunnerTaskStatus.SLURM_RUNNING,
            RunnerTaskStatus.RUNNING,
            RunnerTaskStatus.TIMEOUT,
        }
    ),
    RunnerTaskStatus.SLURM_RUNNING: frozenset(
        {
            RunnerTaskStatus.SLURM_RUNNING,
            RunnerTaskStatus.RUNNING,
            RunnerTaskStatus.TIMEOUT,
        }
    ),
}


class FileTransferStatus(StrEnum):
    CREATED = "created"
    SOURCE_UPLOADING = "source_uploading"
    UPLOADED = "uploaded"
    TARGET_DOWNLOADING = "target_downloading"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class FileTransferRequestType(StrEnum):
    RUNNER_TO_RUNNER = "runner_to_runner"
    RUNNER_TO_UI = "runner_to_ui"


class FileTransferRole(StrEnum):
    SOURCE = "source"
    TARGET = "target"


FILE_TRANSFER_READY_STATUSES = frozenset(
    {
        FileTransferStatus.UPLOADED,
        FileTransferStatus.TARGET_DOWNLOADING,
        FileTransferStatus.COMPLETED,
    }
)
FILE_TRANSFER_TERMINAL_FAILURE_STATUSES = frozenset(
    {FileTransferStatus.FAILED, FileTransferStatus.EXPIRED}
)
