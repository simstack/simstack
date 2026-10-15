from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core.node_runner import NodeRunner
from simstack.runner_runtime.execution_adapter import RunnerExecutionAdapterConfig


def _runner_context(tmp_path):
    return SimpleNamespace(
        initialized=True,
        runner_mode=True,
        config=SimpleNamespace(
            workdir=tmp_path / "work",
            max_inline_document_bytes=1_000_000,
        ),
        runner_adapter=RunnerExecutionAdapterConfig.from_mapping(
            {
                "resource": {
                    "program": {"program": {"worker_environment_allowlist": []}}
                }
            },
            resource="resource",
        ),
    )


def test_node_runner_process_log_name_is_hashed_private_and_contained(
    monkeypatch, tmp_path
):
    fake_context = _runner_context(tmp_path)
    task_dir = fake_context.config.workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    monkeypatch.setenv("TOTALLY_CUSTOM_SECRET", "must-not-reach-workflow")
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["workflow"], 0, stdout="ok", stderr=""
        )
    )
    monkeypatch.setattr("simstack.core.node_runner.subprocess.run", run)
    runner = NodeRunner("program", "task-1")

    assert runner.subprocess("../../outside\nname", ["workflow"]) is True

    component = hashlib.sha256(b"../../outside\nname").hexdigest()
    log_paths = list(task_dir.glob(f"process-{component}-*.log"))
    assert len(log_paths) == 1
    log_path = log_paths[0]
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert not (tmp_path / "outside\nname.log").exists()
    assert run.call_args.kwargs["shell"] is False
    assert "TOTALLY_CUSTOM_SECRET" not in run.call_args.kwargs["env"]


def test_node_runner_repeated_process_names_keep_separate_logs(monkeypatch, tmp_path):
    fake_context = _runner_context(tmp_path)
    task_dir = fake_context.config.workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["workflow"], 0, stdout="ok", stderr=""
        )
    )
    monkeypatch.setattr("simstack.core.node_runner.subprocess.run", run)
    runner = NodeRunner("program", "task-1")

    assert runner.subprocess("same-name", ["workflow"]) is True
    first_log = next(task_dir.glob("process-*.log"))
    first_content = first_log.read_bytes()
    assert runner.subprocess("same-name", ["workflow"]) is True

    assert run.call_count == 2
    assert len(list(task_dir.glob("process-*.log"))) == 2
    assert first_log.read_bytes() == first_content
    assert len(runner.info_files) == 2

    # A fresh worker can reclaim the same task and use its persistent workdir.
    replacement = NodeRunner("program", "task-1")
    assert replacement.subprocess("same-name", ["workflow"]) is True
    assert run.call_count == 3
    assert len(list(task_dir.glob("process-*.log"))) == 3
    assert first_log.read_bytes() == first_content


def test_node_runner_log_open_still_rejects_existing_files_and_symlinks(tmp_path):
    existing = tmp_path / "existing.log"
    existing.write_text("original")
    with pytest.raises(FileExistsError):
        NodeRunner._open_private_log(existing)
    assert existing.read_text() == "original"

    link = tmp_path / "link.log"
    link.symlink_to(existing)
    with pytest.raises(FileExistsError):
        NodeRunner._open_private_log(link)
    assert existing.read_text() == "original"


def test_reclaimed_worker_repeats_real_subprocess_without_overwriting_logs(
    monkeypatch, tmp_path
):
    fake_context = _runner_context(tmp_path)
    task_dir = fake_context.config.workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    first = NodeRunner("program", "task-1")
    assert first.subprocess("step", [sys.executable, "-c", "print('first')"])
    original = next(task_dir.glob("process-*.log"))
    original_content = original.read_bytes()

    replacement = NodeRunner("program", "task-1")
    assert replacement.subprocess("step", [sys.executable, "-c", "print('retry')"])
    assert replacement.subprocess("step", [sys.executable, "-c", "print('next')"])
    assert replacement.last_stdout.strip() == "next"
    assert len(list(task_dir.glob("process-*.log"))) == 3
    assert original.read_bytes() == original_content


def test_watchdog_repeated_names_keep_unique_logs_and_jobs(monkeypatch, tmp_path):
    fake_context = _runner_context(tmp_path)
    task_dir = fake_context.config.workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    submit = MagicMock(
        return_value=SimpleNamespace(status="ok", returncode=0, stdout="ok", stderr="")
    )
    monkeypatch.setattr("simstack.runner_runtime.watchdog.submit_to_watchdog", submit)
    runner = NodeRunner("program", "task-1")
    assert runner.submit_to_watchdog("step", ["workflow"])
    original = next(task_dir.glob("process-*.log"))
    content = original.read_bytes()
    assert runner.submit_to_watchdog("step", ["workflow"])
    assert len(list(task_dir.glob("process-*.log"))) == 2
    assert original.read_bytes() == content
    assert len(runner.info_files) == 2
    assert submit.call_args_list[0].args[1] != submit.call_args_list[1].args[1]


def test_node_runner_rejects_shell_string_before_creating_log(monkeypatch, tmp_path):
    fake_context = _runner_context(tmp_path)
    task_dir = fake_context.config.workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr("simstack.core.context.context", fake_context)
    runner = NodeRunner("program", "task-1")

    with pytest.raises(ValueError, match="argv lists"):
        runner.subprocess("unsafe", "workflow; curl attacker")

    assert list(task_dir.glob("process-*.log")) == []
