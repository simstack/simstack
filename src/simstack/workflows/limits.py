"""Operator budgets for installing code, not limits on workflow result data."""

from dataclasses import asdict, dataclass, fields
from collections.abc import Mapping


@dataclass(frozen=True)
class WorkflowInstallationLimits:
    max_archive_bytes: int = 256 * 1024**2
    max_upload_overhead_bytes: int = 64 * 1024
    max_parallel_uploads: int = 2
    max_upload_seconds: int = 300
    max_unpacked_bytes: int = 1024**3
    max_files: int = 20_000
    max_path_bytes: int = 4096
    max_path_depth: int = 64
    max_manifest_bytes: int = 1024**2
    max_output_bytes: int = 16 * 1024**2
    max_storage_bytes: int = 8 * 1024**3
    max_storage_files: int = 200_000
    max_process_memory_bytes: int = 4 * 1024**3
    max_process_tree_memory_bytes: int = 4 * 1024**3
    max_process_tree_cpu_cores: int = 2
    max_process_cpu_seconds: int = 300
    max_process_file_bytes: int = 1024**3
    max_process_tmp_bytes: int = 256 * 1024**2
    max_processes: int = 128
    max_parallel_processes: int = 2

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")

    @classmethod
    def from_mapping(cls, value: Mapping | None = None):
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("Workflow installation limits must be a table")
        return cls(**dict(value))

    def archive_options(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in (
            "max_archive_bytes", "max_unpacked_bytes", "max_files",
            "max_path_bytes", "max_path_depth", "max_manifest_bytes",
        )}

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def native_thread_environment(self) -> dict[str, str]:
        """Share the operator CPU budget across concurrent scientific helpers."""
        threads = str(max(1, self.max_process_tree_cpu_cores // self.max_parallel_processes))
        return {name: threads for name in (
            "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "OMP_THREAD_LIMIT",
            "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
            "NUMEXPR_NUM_THREADS", "NUMEXPR_MAX_THREADS",
        )}


DEFAULT_INSTALLATION_LIMITS = WorkflowInstallationLimits()
