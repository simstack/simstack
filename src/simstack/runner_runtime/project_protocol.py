"""Verify a pinned workflow's declared SDK contract before loading its code."""

from pathlib import Path
import tomllib

from simstack.runner_runtime.core_build import PROTOCOL_VERSION


def verify_workflow_protocol(source: Path) -> dict:
    path = Path(source) / "pyproject.toml"
    try:
        with path.open("rb") as stream:
            manifest = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ValueError("Workflow pyproject.toml must declare [tool.simstack] runner-protocol") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("Workflow pyproject.toml is not valid TOML") from exc
    tools = manifest.get("tool", {})
    settings = tools.get("simstack", {}) if isinstance(tools, dict) else None
    protocol = settings.get("runner-protocol") if isinstance(settings, dict) else None
    if type(protocol) is not int or protocol != PROTOCOL_VERSION:
        raise ValueError(f"Workflow requires an explicit [tool.simstack] runner-protocol = {PROTOCOL_VERSION}")
    return manifest
