from __future__ import annotations

import hashlib
import importlib
import inspect
import keyword
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from odmantic import ObjectId

from simstack.models.generated_workflow import GeneratedWorkflowSource


GENERATED_NAMESPACE_ROOT = "simstack_generated"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_SLUG_CHARACTERS = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class GeneratedWorkflowMaterialization:
    root: Path
    file_path: Path
    module_path: str


def canonical_source_sha256(source_code: str) -> str:
    return hashlib.sha256(source_code.encode("utf-8")).hexdigest()


def _validate_source_sha256(source_sha256: str) -> str:
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 hex digest")
    return source_sha256


def canonical_generated_module_name(
    workflow_id: str,
    revision: int,
    source_sha256: str,
) -> str:
    """Build the only accepted module name for an immutable source revision."""

    if revision < 1:
        raise ValueError("revision must be at least 1")
    digest = _validate_source_sha256(source_sha256)
    slug = _UNSAFE_SLUG_CHARACTERS.sub("_", workflow_id.strip()).strip("_").lower()
    if not slug:
        raise ValueError("workflow_id must contain at least one letter or digit")
    # The id digest prevents collisions caused by slug normalization or
    # truncation (for example ``a-b`` versus ``a_b``).
    slug = slug[:48].rstrip("_")
    workflow_digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:8]
    return f"workflow_{slug}_{workflow_digest}_r{revision}_{digest[:12]}"


def canonical_generated_namespace(namespace: str) -> str:
    """Validate the dedicated namespace used by generated workflow modules."""

    value = namespace.strip()
    parts = value.split(".")
    if (
        not value
        or parts[0] != GENERATED_NAMESPACE_ROOT
        or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts)
    ):
        raise ValueError(
            "namespace must be a dotted Python path rooted at "
            f"'{GENERATED_NAMESPACE_ROOT}'"
        )
    return value


def generated_module_path(source: GeneratedWorkflowSource) -> str:
    namespace = canonical_generated_namespace(source.namespace)
    expected_module_name = canonical_generated_module_name(
        source.workflow_id,
        source.revision,
        source.source_sha256,
    )
    if source.module_name != expected_module_name:
        raise ValueError(
            f"module_name must be '{expected_module_name}' for this source revision"
        )
    if not source.entrypoint_name.isidentifier() or keyword.iskeyword(
        source.entrypoint_name
    ):
        raise ValueError("entrypoint_name must be a Python identifier")
    return f"{namespace}.{source.module_name}"


def generated_workflow_root() -> Path:
    configured = os.environ.get("SIMSTACK_GENERATED_WORKFLOW_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".cache" / "simstack" / "generated-workflows").resolve()


def _safe_source_path(source: GeneratedWorkflowSource, root: Path) -> Path:
    module_path = generated_module_path(source)
    resolved_root = root.expanduser().resolve()
    file_path = resolved_root.joinpath(*module_path.split(".")).with_suffix(".py")
    resolved_parent = file_path.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(resolved_root):
        raise ValueError("generated workflow path escapes its configured root")
    return file_path


def _file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_source(file_path: Path, source_code: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.is_symlink():
        raise ValueError(f"refusing to replace generated workflow symlink: {file_path}")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.",
        suffix=".tmp",
        dir=file_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(source_code.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, file_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def materialize_generated_workflow_source(
    source: GeneratedWorkflowSource,
    *,
    root: Path | None = None,
) -> GeneratedWorkflowMaterialization:
    """Atomically install one verified revision as an ordinary Python file."""

    expected_sha256 = canonical_source_sha256(source.source_code)
    if source.source_sha256 != expected_sha256:
        raise ValueError(
            "source_code does not match source_sha256: "
            f"expected {expected_sha256}, got {source.source_sha256}"
        )

    materialization_root = (root or generated_workflow_root()).resolve()
    file_path = _safe_source_path(source, materialization_root)
    if file_path.exists():
        if not file_path.is_file() or _file_sha256(file_path) != source.source_sha256:
            raise ValueError(
                f"immutable generated workflow path contains different content: {file_path}"
            )
    else:
        _atomic_write_source(file_path, source.source_code)

    if _file_sha256(file_path) != source.source_sha256:
        raise ValueError(
            f"materialized source failed SHA-256 verification: {file_path}"
        )

    root_string = str(materialization_root)
    if root_string not in sys.path:
        sys.path.insert(0, root_string)
    importlib.invalidate_caches()
    return GeneratedWorkflowMaterialization(
        root=materialization_root,
        file_path=file_path,
        module_path=generated_module_path(source),
    )


def _annotate_generated_members(
    module: ModuleType,
    source: GeneratedWorkflowSource,
) -> None:
    metadata = {
        "_simstack_source_revision": source.id,
        "_simstack_source_sha256": source.source_sha256,
    }
    setattr(module, "_simstack_source_revision", source.id)
    setattr(module, "_simstack_source_sha256", source.source_sha256)
    for _, member in inspect.getmembers(
        module,
        lambda value: inspect.isfunction(value) or inspect.isclass(value),
    ):
        if getattr(member, "__module__", None) != module.__name__:
            continue
        for attribute, value in metadata.items():
            setattr(member, attribute, value)
        inner = getattr(member, "_inner", None)
        if inspect.isfunction(inner):
            for attribute, value in metadata.items():
                setattr(inner, attribute, value)


def import_materialized_generated_module(
    source: GeneratedWorkflowSource,
    *,
    root: Path | None = None,
) -> ModuleType:
    materialized = materialize_generated_workflow_source(source, root=root)
    existing = sys.modules.get(materialized.module_path)
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if (
            existing_file is None
            or Path(existing_file).resolve() != materialized.file_path
        ):
            raise ImportError(
                f"module {materialized.module_path} is already loaded from a different file"
            )
        module = existing
    else:
        module = importlib.import_module(materialized.module_path)

    module_file = getattr(module, "__file__", None)
    if module_file is None or Path(module_file).resolve() != materialized.file_path:
        raise ImportError(
            f"module {materialized.module_path} was not imported from the exact source file"
        )
    if _file_sha256(materialized.file_path) != source.source_sha256:
        raise ImportError(
            "generated workflow source changed while it was being imported"
        )
    _annotate_generated_members(module, source)
    return module


async def resolve_generated_source_for_mapping(
    db: Any,
    mapping: str,
    *,
    source_revision: ObjectId | None = None,
    source_sha256: str | None = None,
) -> GeneratedWorkflowSource | None:
    """Resolve a generated mapping without a name-based revision fallback."""

    if source_revision is not None:
        source = await db.find_one(
            GeneratedWorkflowSource,
            GeneratedWorkflowSource.id == source_revision,
        )
        if source is None:
            raise LookupError(f"generated source revision {source_revision} not found")
        if source_sha256 is not None and source.source_sha256 != source_sha256:
            raise LookupError(
                f"generated source revision {source_revision} has a different SHA-256"
            )
        expected_module = generated_module_path(source)
        if not mapping.startswith(expected_module + "."):
            raise LookupError(
                f"mapping '{mapping}' does not belong to source revision {source_revision}"
            )
        return source

    try:
        module_path, _ = mapping.rsplit(".", 1)
        namespace, module_name = module_path.rsplit(".", 1)
    except ValueError:
        return None
    if not namespace.startswith(GENERATED_NAMESPACE_ROOT):
        return None
    return await db.find_one(
        GeneratedWorkflowSource,
        (GeneratedWorkflowSource.namespace == namespace)
        & (GeneratedWorkflowSource.module_name == module_name),
    )


async def import_generated_symbol(
    db: Any,
    mapping: str,
    *,
    source_revision: ObjectId | None = None,
    source_sha256: str | None = None,
) -> Any | None:
    source = await resolve_generated_source_for_mapping(
        db,
        mapping,
        source_revision=source_revision,
        source_sha256=source_sha256,
    )
    if source is None:
        return None
    module_path, symbol_name = mapping.rsplit(".", 1)
    module = import_materialized_generated_module(source)
    if module.__name__ != module_path:
        raise LookupError(
            f"generated mapping '{mapping}' resolved to a different module"
        )
    try:
        return getattr(module, symbol_name)
    except AttributeError as exc:
        raise LookupError(f"generated symbol '{mapping}' is not exposed") from exc
