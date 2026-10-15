from __future__ import annotations

import os
import signal
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.runner_runtime.watchdog.queue import process_job_from_claim, run_command


def test_watchdog_executes_argv_behind_guard_with_exact_environment(
    monkeypatch, tmp_path
):
    process = SimpleNamespace(wait=MagicMock(return_value=0))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(
        "simstack.runner_runtime.watchdog.queue.subprocess.Popen", popen
    )
    out_file = tmp_path / "job.stdout"
    err_file = tmp_path / "job.stderr"

    returncode = run_command(
        ["workflow", "--input", "value with spaces"],
        ["network-guard", "--"],
        str(tmp_path),
        {"PATH": "/usr/bin", "LICENSE": "value"},
        30,
        out_file,
        err_file,
    )

    assert returncode == 0
    assert popen.call_args.args[0] == [
        "network-guard",
        "--",
        "workflow",
        "--input",
        "value with spaces",
    ]
    assert popen.call_args.kwargs["shell"] is False
    assert popen.call_args.kwargs["env"] == {
        "PATH": "/usr/bin",
        "LICENSE": "value",
    }
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(err_file.stat().st_mode) == 0o600


def test_watchdog_timeout_terminates_then_kills_process_group(monkeypatch, tmp_path):
    process = SimpleNamespace(
        pid=4321,
        poll=MagicMock(return_value=None),
        wait=MagicMock(
            side_effect=[
                subprocess.TimeoutExpired(["workflow"], 1),
                subprocess.TimeoutExpired(["workflow"], 5),
                0,
            ]
        ),
        terminate=MagicMock(),
        kill=MagicMock(),
    )
    monkeypatch.setattr(
        "simstack.runner_runtime.watchdog.queue.subprocess.Popen",
        MagicMock(return_value=process),
    )
    killpg = MagicMock()
    monkeypatch.setattr("simstack.runner_runtime.watchdog.queue.os.killpg", killpg)

    returncode = run_command(
        ["workflow"],
        ["network-guard"],
        None,
        {"PATH": "/usr/bin"},
        1,
        tmp_path / "job.stdout",
        tmp_path / "job.stderr",
    )

    assert returncode == 124
    if os.name != "nt":
        assert killpg.call_args_list == [
            ((4321, signal.SIGTERM),),
            ((4321, signal.SIGKILL),),
        ]


def test_invalid_watchdog_spec_writes_immediate_private_failure(tmp_path):
    base = tmp_path / "job"
    base.with_suffix(".cmd").write_text("not-json", encoding="utf-8")
    base.with_suffix(".claim").touch()

    process_job_from_claim(base, ["network-guard"])

    assert not base.with_suffix(".cmd").exists()
    assert not base.with_suffix(".claim").exists()
    assert base.with_suffix(".fail").exists()
    assert base.with_suffix(".exit").read_text(encoding="utf-8") == "1\n"
    assert "Invalid job specification" in base.with_suffix(".stderr").read_text(
        encoding="utf-8"
    )
    for suffix in (".fail", ".exit", ".stderr"):
        assert stat.S_IMODE(base.with_suffix(suffix).stat().st_mode) == 0o600


def test_watchdog_refuses_to_run_without_network_guard(tmp_path):
    with pytest.raises(ValueError, match="network guard"):
        run_command(
            ["workflow"],
            [],
            None,
            {"PATH": "/usr/bin"},
            1,
            tmp_path / "job.stdout",
            tmp_path / "job.stderr",
        )

    assert not (tmp_path / "job.stdout").exists()
    assert not (tmp_path / "job.stderr").exists()
