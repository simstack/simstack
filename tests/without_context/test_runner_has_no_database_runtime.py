from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
_RUNNER_PACKAGE_ROOTS = (
    _SOURCE_ROOT / "simstack" / "runner_api",
    _SOURCE_ROOT / "simstack" / "runner_protocol",
    _SOURCE_ROOT / "simstack" / "runner_runtime",
    _SOURCE_ROOT / "simstack" / "core" / "runner_execution",
    _SOURCE_ROOT / "simstack" / "core" / "services",
)
_RUNNER_ENTRYPOINTS = (
    _SOURCE_ROOT / "simstack" / "core" / "node.py",
    _SOURCE_ROOT / "simstack" / "core" / "runner.py",
    _SOURCE_ROOT / "simstack" / "core" / "run_node.py",
    _SOURCE_ROOT / "simstack" / "core" / "run_docker.py",
    _SOURCE_ROOT / "simstack" / "core" / "submit_node.py",
    _SOURCE_ROOT / "simstack" / "core" / "node_runner.py",
)
_LEGACY_RUNNER_MODULES = (
    "simstack.core.definitions",
    "simstack.core.run_node_protocol",
    "simstack.logging_utils",
    "simstack.core.runner_task_execution",
    "simstack.core.services.runner_cleanup_service",
    "simstack.methods.archive_files",
    "simstack.methods.fire_and_forget_runner",
    "simstack.methods.mass_runner",
    "simstack.methods.multiple_executor",
    "simstack.util.db_logger",
    "simstack.util.file_transfer_client",
    "simstack.util.queue_watcher",
    "simstack.util.runner_api_client",
    "simstack.util.runner_api_log_handler",
    "simstack.util.runner_config",
    "simstack.util.runner_environment",
    "simstack.util.runner_util",
    "simstack.util.runner_utils",
    "simstack.util.submit_to_watchdog",
)


def _production_runner_sources() -> tuple[Path, ...]:
    package_sources = {
        source
        for package_root in _RUNNER_PACKAGE_ROOTS
        for source in package_root.rglob("*.py")
    }
    return tuple(sorted(package_sources | set(_RUNNER_ENTRYPOINTS)))


def _module_name(source_path: Path) -> str:
    relative = source_path.relative_to(_SOURCE_ROOT).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


_PRODUCTION_RUNNER_SOURCES = _production_runner_sources()
_PRODUCTION_RUNNER_MODULES = tuple(
    _module_name(source_path) for source_path in _PRODUCTION_RUNNER_SOURCES
)


@pytest.mark.parametrize(
    "source_path",
    _PRODUCTION_RUNNER_SOURCES,
    ids=lambda source_path: str(source_path.relative_to(_SOURCE_ROOT)),
)
def test_production_runner_module_has_no_direct_database_path(source_path):
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    forbidden_packages = {"bson", "motor", "odmantic", "pymongo"}
    forbidden_simstack_modules = {
        "simstack.persistence",
        "simstack.util.db",
        "simstack.util.db_logger",
        "simstack.util.mock_db",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    alias.name.split(".", 1)[0] in forbidden_packages
                    or alias.name in forbidden_simstack_modules
                ):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if (
                node.module.split(".", 1)[0] in forbidden_packages
                or node.module in forbidden_simstack_modules
            ):
                offenders.append(f"line {node.lineno}: from {node.module}")
            if node.module == "simstack.util" and any(
                alias.name in {"db", "db_logger", "mock_db"} for alias in node.names
            ):
                offenders.append(f"line {node.lineno}: from simstack.util")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in {"db", "repository"}
            and isinstance(node.value, ast.Name)
            and node.value.id == "context"
        ):
            offenders.append(f"line {node.lineno}: context.{node.attr}")

    assert offenders == []


def test_runner_modules_import_when_database_packages_are_forbidden():
    """The shipped runner runtime must not import a Mongo/ODM package at all."""

    project_root = _PROJECT_ROOT
    script = r"""
import builtins
import sys

blocked = {"bson", "motor", "odmantic", "pymongo"}
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".", 1)[0] in blocked:
        raise AssertionError(f"runner imported forbidden database package: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import

import importlib

modules = %r
for module_name in modules:
    importlib.import_module(module_name)
import simstack.core.node

persistence_only_modules = {
    "simstack.persistence",
    "simstack.persistence.criteria",
    "simstack.persistence.repository",
    "simstack.core.artifacts",
    "simstack.core.node_persistence",
    "simstack.core.process_results",
    "simstack.core.recompute_artifacts",
    "simstack.core.services.mapping_registry_service",
}
unexpected = sorted(persistence_only_modules.intersection(sys.modules))
if unexpected:
    raise AssertionError(
        f"runner imported persistence-only execution modules: {unexpected}"
    )
"""
    script = script % (_PRODUCTION_RUNNER_MODULES,)
    environment = os.environ.copy()
    source_path = str(project_root / "src")
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_path
        else os.pathsep.join((source_path, existing_path))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.parametrize("module_name", _LEGACY_RUNNER_MODULES)
def test_deleted_runner_compatibility_module_is_absent(module_name):
    assert importlib.util.find_spec(module_name) is None


def test_protocol_route_and_scope_literals_have_one_source():
    from simstack.runner_protocol import (
        FILE_TRANSFER_API_PREFIX,
        RUNNER_API_PREFIX,
        RunnerScope,
    )

    forbidden_literals = {
        RUNNER_API_PREFIX,
        FILE_TRANSFER_API_PREFIX,
        *(scope.value for scope in RunnerScope),
    }
    protocol_root = _SOURCE_ROOT / "simstack" / "runner_protocol"
    offenders: list[str] = []
    for source_path in (_SOURCE_ROOT / "simstack").rglob("*.py"):
        if source_path.is_relative_to(protocol_root):
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in forbidden_literals:
                offenders.append(
                    f"{source_path.relative_to(_SOURCE_ROOT)}:{node.lineno}: "
                    f"{node.value!r}"
                )

    assert offenders == []


def test_runner_execution_adapter_keys_have_one_source():
    adapter_source = (
        _SOURCE_ROOT / "simstack" / "runner_runtime" / "execution_adapter.py"
    )
    adapter_keys = {
        "container_backend",
        "container_command",
        "container_image",
        "container_project_root",
        "container_worker_command",
        "container_workdir",
        "local_worker_command",
        "slurm_status_command",
        "slurm_submit_command",
        "slurm_worker_command",
        "tmp_base_dir",
        "use_tmp",
        "worker_environment_allowlist",
        "worker_network_guard_command",
    }
    offenders: list[str] = []
    for source_path in _PRODUCTION_RUNNER_SOURCES:
        if source_path == adapter_source:
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in adapter_keys:
                offenders.append(
                    f"{source_path.relative_to(_SOURCE_ROOT)}:{node.lineno}: "
                    f"{node.value!r}"
                )

    assert offenders == []


def test_domain_status_and_telemetry_enums_are_protocol_aliases():
    from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
    from simstack.models.runner_model import RunnerEventEnum, RunnerType
    from simstack.runner_protocol import (
        RunnerEventType,
        RunnerProcessType,
        RunnerTaskStatus,
    )

    assert TaskStatus is RunnerTaskStatus
    assert RunnerEventEnum is RunnerEventType
    assert RunnerType is RunnerProcessType
