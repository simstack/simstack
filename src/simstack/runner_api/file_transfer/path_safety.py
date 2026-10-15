"""Resource identity and workdir-contained path helpers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from simstack.runner_protocol import RUNNER_CURRENT_RESOURCE_ALIAS

from .transport import FileTransferError


def path_for_file_instance(path: Path, workdir: Path) -> str:
    resolved_path = Path(path).resolve()
    resolved_workdir = Path(workdir).resolve()
    try:
        return str(resolved_path.relative_to(resolved_workdir))
    except ValueError as exc:
        raise FileTransferError(
            f"File instance path escapes runner workdir: {resolved_path}"
        ) from exc


def resolve_instance_path(raw_path: str, workdir: Path) -> Path:
    resolved_workdir = Path(workdir).resolve()
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise FileTransferError("File transfer source path must be canonical relative")
    posix_path = PurePosixPath(raw_path)
    windows_path = PureWindowsPath(raw_path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or "\\" in raw_path
        or any(part in {".", ".."} for part in posix_path.parts)
        or str(posix_path) != raw_path
    ):
        raise FileTransferError("File transfer source path must be canonical relative")
    resolved_path = (resolved_workdir / Path(raw_path)).resolve()
    try:
        resolved_path.relative_to(resolved_workdir)
    except ValueError as exc:
        raise FileTransferError(
            f"File transfer source path escapes runner workdir: {raw_path!r}"
        ) from exc
    return resolved_path


def resource_name(value: Any) -> str:
    raw = getattr(value, "__dict__", {}).get("value")
    return str(raw) if raw is not None else str(value)


def transfer_resource_name(value: Any) -> str:
    name = resource_name(value)
    if name != RUNNER_CURRENT_RESOURCE_ALIAS:
        return name

    from simstack.core.context import context

    if not context.runner_mode:
        raise FileTransferError(
            "Cannot resolve symbolic resource 'self' outside API-only runner mode"
        )
    return str(context.config.resource)


def first_available_remote_location(
    locations: Iterable[Any], local_resource: Any
) -> Any | None:
    local_resource_str = transfer_resource_name(local_resource)
    for location in locations:
        if (
            transfer_resource_name(getattr(location, "resource", ""))
            == local_resource_str
        ):
            continue
        if getattr(location, "status", "available") != "available":
            continue
        return location
    return None
