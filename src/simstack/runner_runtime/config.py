"""Server-owned configuration projected into the API-only runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from simstack.models.parameters import Resource
from simstack.runner_protocol import (
    RunnerProtocolError,
    validate_runner_capabilities_v1,
    validate_runner_resource_v1,
)


def _configured_path(raw_path: str, project_root: Path) -> Path:
    replacements = {
        "$HOME": os.environ.get("HOME", str(Path.home())),
        "$TEMP": os.environ.get("TEMP", os.environ.get("TMP", "/tmp")),
        "$PROJECT": os.environ.get("PROJECT", str(project_root)),
    }
    value = str(raw_path)
    for variable, replacement in replacements.items():
        value = value.replace(variable, replacement)
    path = Path(os.path.expanduser(os.path.expandvars(value)))
    if not path.is_absolute():
        path = project_root / path
    return path


def runner_task_workdir(workdir: Path, task_id: str) -> Path:
    """Return a task-id-only directory contained by the configured workdir."""

    task_component = str(task_id)
    if (
        not task_component
        or task_component in {".", ".."}
        or Path(task_component).name != task_component
        or "/" in task_component
        or "\\" in task_component
    ):
        raise ValueError("Runner task id must be a single path component")
    resolved_workdir = Path(workdir).resolve()
    resolved_task_dir = (resolved_workdir / "tasks" / task_component).resolve()
    try:
        resolved_task_dir.relative_to(resolved_workdir)
    except ValueError as exc:  # pragma: no cover - protected by component check
        raise ValueError("Runner task directory escapes configured workdir") from exc
    return resolved_task_dir


@dataclass(frozen=True)
class RunnerConfig:
    """Local view of server-owned runner bootstrap configuration."""

    resource_definition: Mapping[str, Any]
    project_root: Path
    server_url: str
    workdir_override: Path | None = None
    max_runner_payload_bytes: int | None = None
    max_inline_document_bytes: int = 0
    workflow_installations: bool = False

    @classmethod
    def from_bootstrap(
        cls,
        payload: Mapping[str, Any],
        *,
        project_root: Path,
        server_url: str,
        workdir_override: Path | None = None,
    ) -> "RunnerConfig":
        try:
            resource = validate_runner_resource_v1(
                payload.get("resource"), operation="bootstrap"
            )
        except RunnerProtocolError as exc:
            raise ValueError(str(exc)) from exc
        try:
            max_runner_payload_bytes, max_inline_document_bytes = (
                validate_runner_capabilities_v1(
                    payload.get("capabilities"), operation="bootstrap"
                )
            )
        except RunnerProtocolError as exc:
            raise ValueError(str(exc)) from exc
        return cls(
            resource_definition=dict(resource),
            project_root=Path(project_root).resolve(),
            server_url=server_url,
            workdir_override=(
                Path(workdir_override).resolve()
                if workdir_override is not None
                else None
            ),
            max_runner_payload_bytes=max_runner_payload_bytes,
            max_inline_document_bytes=max_inline_document_bytes,
            workflow_installations=bool(payload.get("capabilities", {}).get("workflow_installations", False)),
        )

    @property
    def resource(self) -> Resource:
        return Resource(value=str(self.resource_definition["resource_str"]))

    @property
    def workdir(self) -> Path:
        if self.workdir_override is not None:
            return self.workdir_override
        return _configured_path(
            str(self.resource_definition["workdir"]), self.project_root
        )

    @property
    def environment_start(self) -> str | None:
        value = self.resource_definition.get("environment_start")
        return str(value) if value is not None else None

    @property
    def python_paths(self) -> list[Path]:
        values = self.resource_definition.get("python_paths", [])
        return [_configured_path(str(value), self.project_root) for value in values]

    @property
    def hostname(self) -> str:
        return str(self.resource_definition["hostname"])

    @property
    def is_default(self) -> bool:
        return bool(self.resource_definition.get("is_default", False))
