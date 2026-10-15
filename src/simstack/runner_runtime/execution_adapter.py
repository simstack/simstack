"""Typed, operator-owned process adapters for the API-only runner.

This module is the only runner component that knows the layout of local
``config.toml``.  Control-plane state still comes exclusively from the server
API; this file contains only argv, environment allowlists and local filesystem
locations needed to execute work on a particular resource.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from simstack.runner_runtime.environment import (
    validate_worker_environment_allowlist,
)
from simstack.workflows.limits import WorkflowInstallationLimits


class ContainerBackend(str, Enum):
    """Container engines supported by the local execution adapter."""

    DOCKER = "docker"
    APPTAINER = "apptainer"


@dataclass(frozen=True)
class WorkerBoundary:
    """Process isolation contract shared by all worker launchers."""

    network_guard_command: tuple[str, ...]
    environment_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowInstallationAdapter:
    """Operator-owned interpreter and process boundary for package preparation."""

    base_python: str
    boundary: WorkerBoundary
    limits: WorkflowInstallationLimits = WorkflowInstallationLimits()


@dataclass(frozen=True)
class LocalExecutionAdapter:
    worker_command: tuple[str, ...]
    boundary: WorkerBoundary

    @property
    def guarded_worker_command(self) -> tuple[str, ...]:
        return (*self.boundary.network_guard_command, *self.worker_command)


@dataclass(frozen=True)
class ContainerExecutionAdapter:
    backend: ContainerBackend
    launcher_command: tuple[str, ...]
    worker_command: tuple[str, ...]
    boundary: WorkerBoundary
    image: str
    project_root: PurePosixPath
    workdir: PurePosixPath

    @property
    def guarded_worker_command(self) -> tuple[str, ...]:
        return (*self.boundary.network_guard_command, *self.worker_command)


@dataclass(frozen=True)
class SlurmExecutionAdapter:
    submit_command: tuple[str, ...]
    worker_command: tuple[str, ...]
    boundary: WorkerBoundary
    use_tmp: bool
    tmp_base_dir: Path | None

    @property
    def guarded_worker_command(self) -> tuple[str, ...]:
        return (*self.boundary.network_guard_command, *self.worker_command)

    def task_tmp_dir(self, task_id: object) -> Path:
        if self.tmp_base_dir is None:
            raise RuntimeError(
                "Slurm temporary recovery requires an explicit tmp_base_dir"
            )
        component = _task_component(task_id)
        task_dir = (self.tmp_base_dir / component).resolve()
        try:
            task_dir.relative_to(self.tmp_base_dir.resolve())
        except ValueError as exc:  # pragma: no cover - guarded by _task_component
            raise ValueError(
                "Runner temporary directory escaped its configured root"
            ) from exc
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return task_dir


@dataclass(frozen=True)
class SlurmStatusAdapter:
    command_template: tuple[str, ...]

    def command_for(self, job_id: str) -> tuple[str, ...]:
        if not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdigit():
            raise ValueError("Slurm job id must contain only ASCII digits")
        return tuple(item.replace("{job_id}", job_id) for item in self.command_template)


@dataclass(frozen=True)
class RunnerExecutionAdapterConfig:
    """Typed projection of one resource's local execution-adapter config."""

    _resource: Mapping[str, object]
    _config_directory: Path

    @classmethod
    def from_file(
        cls, config_path: str | os.PathLike[str], resource: str
    ) -> "RunnerExecutionAdapterConfig":
        actual_path = Path(config_path)
        if actual_path.is_dir():
            actual_path = actual_path / "config.toml"
        if not actual_path.is_file():
            raise RuntimeError(
                f"Runner execution-adapter config does not exist: {actual_path}"
            )
        try:
            with actual_path.open("rb") as config_file:
                configuration = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(
                f"Runner execution-adapter config is invalid: {actual_path}"
            ) from exc
        return cls.from_mapping(
            configuration,
            resource=resource,
            config_directory=actual_path.parent,
        )

    @classmethod
    def from_mapping(
        cls,
        configuration: Mapping[str, object],
        *,
        resource: str,
        config_directory: str | os.PathLike[str] = ".",
    ) -> "RunnerExecutionAdapterConfig":
        if not isinstance(configuration, Mapping):
            raise RuntimeError("Runner execution-adapter config must be a table")
        if not isinstance(resource, str) or not resource:
            raise RuntimeError("Runner execution-adapter resource must be non-empty")
        if resource not in configuration:
            raise RuntimeError(
                f"Runner execution-adapter resource {resource!r} is not configured"
            )
        raw_resource = configuration[resource]
        resource_table = _table(
            raw_resource,
            contract=f"resource {resource!r}",
        )
        return cls(
            _resource=dict(resource_table),
            _config_directory=Path(config_directory).resolve(),
        )

    def worker_environment(
        self, program_name: str, *, runtime_profile: str | None = None
    ) -> tuple[str, ...]:
        """Return the only ambient variables a workflow may inherit."""

        program = self._program(program_name, runtime_profile=runtime_profile)
        return validate_worker_environment_allowlist(
            program.get("worker_environment_allowlist")
        )

    def local(
        self, program_name: str, *, runtime_profile: str | None = None
    ) -> LocalExecutionAdapter:
        program = self._program(program_name, runtime_profile=runtime_profile)
        return LocalExecutionAdapter(
            worker_command=_command(
                program.get("local_worker_command"),
                name="local_worker_command",
                contract="Local",
            ),
            boundary=self._worker_boundary(program, guard_source=program),
        )

    def container(
        self, program_name: str, *, runtime_profile: str | None = None
    ) -> ContainerExecutionAdapter:
        program = self._program(program_name, runtime_profile=runtime_profile)
        backend_value = _required_string(
            program.get("container_backend"),
            name="container_backend",
            contract="Container",
        )
        try:
            backend = ContainerBackend(backend_value)
        except ValueError as exc:
            raise RuntimeError(
                f"Unsupported container backend {backend_value!r}"
            ) from exc

        project_root = PurePosixPath(
            _required_string(
                program.get("container_project_root"),
                name="container_project_root",
                contract="Container",
            )
        )
        workdir = PurePosixPath(
            _required_string(
                program.get("container_workdir"),
                name="container_workdir",
                contract="Container",
            )
        )
        if not project_root.is_absolute() or not workdir.is_absolute():
            raise RuntimeError(
                "Container project root and workdir must be absolute paths"
            )

        return ContainerExecutionAdapter(
            backend=backend,
            launcher_command=_command(
                program.get("container_command"),
                name="container_command",
                contract="Container",
            ),
            worker_command=_command(
                program.get("container_worker_command"),
                name="container_worker_command",
                contract="Container",
            ),
            boundary=self._worker_boundary(program, guard_source=program),
            image=_required_string(
                program.get("container_image"),
                name="container_image",
                contract="Container",
            ),
            project_root=project_root,
            workdir=workdir,
        )

    def slurm(
        self, program_name: str, *, runtime_profile: str | None = None
    ) -> SlurmExecutionAdapter:
        program = self._program(program_name, runtime_profile=runtime_profile)
        setup = self._setup()
        use_tmp = program.get("use_tmp", False)
        if type(use_tmp) is not bool:
            raise RuntimeError("Slurm use_tmp must be a boolean")
        return SlurmExecutionAdapter(
            submit_command=_command(
                setup.get("slurm_submit_command"),
                name="slurm_submit_command",
                contract="Slurm",
            ),
            worker_command=_command(
                setup.get("slurm_worker_command"),
                name="slurm_worker_command",
                contract="Slurm",
            ),
            boundary=self._worker_boundary(program, guard_source=setup),
            use_tmp=use_tmp,
            tmp_base_dir=self._tmp_base_dir(setup, required=use_tmp),
        )

    def slurm_status(self) -> SlurmStatusAdapter:
        command = _command(
            self._setup().get("slurm_status_command"),
            name="slurm_status_command",
            contract="Slurm status",
        )
        if sum(item.count("{job_id}") for item in command) != 1:
            raise RuntimeError(
                "slurm_status_command must contain exactly one {job_id} placeholder"
            )
        return SlurmStatusAdapter(command_template=command)

    def workflow_profile(self, profile_name: str) -> Mapping[str, object]:
        profiles = _table(
            self._resource.get("workflow_profiles", {}), contract="workflow_profiles"
        )
        if profile_name not in profiles:
            raise RuntimeError(
                f"Workflow runtime profile {profile_name!r} is not configured on this resource"
            )
        return _table(
            profiles[profile_name], contract=f"workflow profile {profile_name!r}"
        )

    def workflow_installer(self, profile_name: str) -> WorkflowInstallationAdapter:
        profile = self.workflow_profile(profile_name)
        return WorkflowInstallationAdapter(
            base_python=_required_string(
                profile.get("installer_python"),
                name="installer_python",
                contract="Workflow installation",
            ),
            boundary=self._worker_boundary(profile, guard_source=profile),
            limits=WorkflowInstallationLimits.from_mapping(profile.get("installation_limits")),
        )

    def _program(
        self, program_name: str, *, runtime_profile: str | None = None
    ) -> Mapping[str, object]:
        if runtime_profile is not None:
            return self.workflow_profile(runtime_profile)
        if not isinstance(program_name, str) or not program_name:
            raise RuntimeError("Runner program name must be non-empty")
        programs = _table(self._resource.get("program", {}), contract="program")
        return _table(
            programs.get(program_name, {}),
            contract=f"program {program_name!r}",
        )

    def _setup(self) -> Mapping[str, object]:
        return _table(self._resource.get("setup", {}), contract="setup")

    def _worker_boundary(
        self,
        program: Mapping[str, object],
        *,
        guard_source: Mapping[str, object],
    ) -> WorkerBoundary:
        return WorkerBoundary(
            network_guard_command=_command(
                guard_source.get("worker_network_guard_command"),
                name="worker_network_guard_command",
                contract="Worker",
            ),
            environment_allowlist=validate_worker_environment_allowlist(
                program.get("worker_environment_allowlist")
            ),
        )

    def _tmp_base_dir(
        self, setup: Mapping[str, object], *, required: bool
    ) -> Path | None:
        raw_path = setup.get("tmp_base_dir")
        if raw_path is None or raw_path == "":
            if required:
                raise RuntimeError(
                    "Slurm use_tmp requires an explicit non-empty tmp_base_dir"
                )
            return None
        if not isinstance(raw_path, str):
            raise RuntimeError("Slurm tmp_base_dir must be a path string")
        expanded = Path(os.path.expanduser(os.path.expandvars(raw_path)))
        if not expanded.is_absolute():
            expanded = self._config_directory / expanded
        return expanded.resolve()


def _table(value: object, *, contract: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Runner execution-adapter {contract} must be a table")
    if not all(isinstance(key, str) for key in value):
        raise RuntimeError(
            f"Runner execution-adapter {contract} contains a non-string key"
        )
    return value


def _command(value: object, *, name: str, contract: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise RuntimeError(f"{contract} launch contract requires non-empty {name} argv")
    return tuple(value)


def _required_string(value: object, *, name: str, contract: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{contract} launch contract requires non-empty {name}")
    return value


def _task_component(task_id: object) -> str:
    component = str(task_id)
    if (
        not component
        or component in {".", ".."}
        or Path(component).name != component
        or "/" in component
        or "\\" in component
    ):
        raise ValueError("Runner task id must be a single path component")
    return component


__all__ = [
    "ContainerBackend",
    "ContainerExecutionAdapter",
    "LocalExecutionAdapter",
    "RunnerExecutionAdapterConfig",
    "SlurmExecutionAdapter",
    "SlurmStatusAdapter",
    "WorkerBoundary",
]
