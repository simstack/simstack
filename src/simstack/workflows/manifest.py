"""Explicit source/runtime identity; derived catalog metadata is not duplicated."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS


def _module_path(value: str) -> bool:
    return isinstance(value, str) and all(
        part.isidentifier() for part in value.split(".")
    )


def canonical_path(value: str) -> str:
    if not _module_path(value) or "." not in value:
        raise ValueError("A mapping must be a canonical module-qualified Python path")
    return value


class ResultDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    mapping: str = Field(min_length=3)
    description: str | None = None


class NodeOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    called_nodes: list[str] | None = None
    results: list[ResultDeclaration] | None = None

    @field_validator("called_nodes")
    @classmethod
    def validate_children(cls, values):
        if values is not None:
            if len(values) != len(set(values)):
                raise ValueError("Explicit called_nodes must be unique")
            return [canonical_path(value) for value in values]
        return values


class ArtifactDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    function_mapping: str
    nodes: list[str]

    @field_validator("function_mapping")
    @classmethod
    def validate_function(cls, value):
        return canonical_path(value)

    @field_validator("nodes")
    @classmethod
    def validate_nodes(cls, values):
        if not values or len(values) != len(set(values)):
            raise ValueError("Artifact nodes must be a non-empty unique list")
        return [canonical_path(value) for value in values]


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    modules: list[str] = Field(min_length=1)
    source_root: str = "."
    runtime_profile: str = Field(min_length=1)
    dependencies: list[str] | None = None
    executables: list[str] = Field(default_factory=list)

    @field_validator("executables")
    @classmethod
    def executable_paths(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Workflow executable paths must be unique")
        for value in values:
            path = PurePosixPath(value)
            if (
                not value or "\\" in value or "\x00" in value or path.is_absolute() or PureWindowsPath(value).drive
                or any(part in {".", "..", ""} for part in value.split("/"))
            ):
                raise ValueError("Workflow executables must be contained relative file paths")
        return values

    @field_validator("id", "version", "runtime_profile")
    @classmethod
    def trimmed(cls, value):
        if value != value.strip():
            raise ValueError(
                "Manifest identifiers must not have surrounding whitespace"
            )
        return value

    @field_validator("modules")
    @classmethod
    def module_paths(cls, values):
        if len(values) != len(set(values)) or any(
            not _module_path(value) for value in values
        ):
            raise ValueError(
                "Workflow modules must be unique canonical Python module paths"
            )
        return values

    @field_validator("source_root")
    @classmethod
    def relative_root(cls, value):
        path = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or path.is_absolute()
            or any(part == ".." for part in path.parts)
        ):
            raise ValueError("source_root must be a contained relative POSIX directory")
        return value

    @field_validator("dependencies")
    @classmethod
    def requirements(cls, values):
        if values is None:
            return values
        normalized = []
        for value in values:
            requirement = Requirement(value)
            if requirement.url is not None:
                raise ValueError(
                    "Workflow dependencies must be index requirements, not URLs or paths"
                )
            if canonicalize_name(requirement.name) in {
                "simstack",
                "odmantic",
                "motor",
                "pymongo",
                "bson",
            }:
                raise ValueError(
                    "Workflow dependencies cannot replace SimStack or install database drivers"
                )
            normalized.append(str(requirement))
        if len(normalized) != len(set(normalized)):
            raise ValueError("Workflow dependencies must be unique")
        return normalized


class WorkflowManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: WorkflowDefinition
    nodes: dict[str, NodeOverride] = Field(default_factory=dict)
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)

    @field_validator("nodes")
    @classmethod
    def node_keys(cls, values):
        for mapping in values:
            canonical_path(mapping)
        return values


def _load_metadata(path: Path, max_bytes: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Workflow metadata exceeds configured manifest size")
    return tomllib.loads(data.decode("utf-8"))


def executable_paths(source: str | Path, *, max_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes) -> list[str]:
    """Read permission declarations without imposing discovery requirements."""
    root = Path(source).resolve(strict=True)
    raw = _load_metadata(root / "simstack-workflow.toml", max_bytes)
    workflow = raw.get("workflow", {})
    if not isinstance(workflow, dict):
        raise ValueError("Workflow metadata must contain a workflow table")
    values = TypeAdapter(list[str]).validate_python(workflow.get("executables", []))
    values = WorkflowDefinition.executable_paths(values)
    for relative in values:
        executable = root / relative
        if executable.is_symlink() or not executable.is_file() or not executable.resolve().is_relative_to(root):
            raise ValueError(f"Workflow executable is not a contained regular file: {relative}")
    return values


def load_manifest(source: str | Path, *, max_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes) -> WorkflowManifest:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Manifest size budget must be a positive integer")
    root = Path(source).resolve(strict=True)
    raw = _load_metadata(root / "simstack-workflow.toml", max_bytes)
    manifest = WorkflowManifest.model_validate(raw)
    import_root = (root / manifest.workflow.source_root).resolve(strict=True)
    if not import_root.is_relative_to(root) or not import_root.is_dir():
        raise ValueError(
            "Manifest source_root is not a directory contained by the snapshot"
        )
    for relative in manifest.workflow.executables:
        executable = root / relative
        if executable.is_symlink() or not executable.is_file() or not executable.resolve().is_relative_to(root):
            raise ValueError(f"Workflow executable is not a contained regular file: {relative}")
    if manifest.workflow.dependencies is None:
        project_path = root / "pyproject.toml"
        dependencies = []
        if project_path.is_file():
            project = _load_metadata(project_path, max_bytes).get("project", {})
            if "dependencies" in project.get("dynamic", []):
                raise ValueError(
                    "Dynamic pyproject dependencies require explicit manifest dependencies"
                )
            dependencies = project.get("dependencies", [])
        manifest.workflow.dependencies = WorkflowDefinition.requirements(dependencies)
    return manifest
