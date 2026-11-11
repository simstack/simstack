from enum import Enum


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SLURM_QUEUED = "slurm_queued"
    SLURM_RUNNING = "slurm_running"
    RETRIEVED = "retrieved"
    TIME_OUT = "timeout"
    FAILED = "failed"
    COMPLETED = "completed"
    RECOVERED = "recovered"
    TERMINATING = "terminating"
