from enum import Enum


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SLURM_QUEUED = "slurm_queued"
    SLURM_RUNNING = "slurm_running"
    TIME_OUT = "timeout"
    FAILED = "failed"
    COMPLETED = "completed"
    RECOVERED = "recovered"


# TODO: eliminate
class DBType(str, Enum):
    SQLITE = "sqlite"
    MONGODB = "mongodb"
    POSTGRES = "postgres"
    IN_MEMORY = "in_memory"
    WITH_PATH = "with_path"
