import os
import time

import pytest

from simstack.core.node_runner import NodeRunner
from simstack.util.resource_config import ResourceConfig


def _staged_runner(tmp_path, monkeypatch):
    from simstack.core.context import context

    scratch_base = tmp_path / "scratch_base"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[self.setup]
tmp_base_dir = "{scratch_base.as_posix()}"
[self.program.orca]
run_command = "echo hi"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "_resource_config", ResourceConfig(config_file, "self"))
    workdir = tmp_path / "work"
    workdir.mkdir()
    runner = NodeRunner("orca", "watch_task")
    return runner, workdir


class TestNodeRunnerFileWatchdog:
    def test_file_watchdog_copies_from_scratch(self, tmp_path, monkeypatch):
        runner, workdir = _staged_runner(tmp_path, monkeypatch)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            scratch = runner.stage()
            runner.file_watchdog(["progress.out"], interval=0.1)
            try:
                (scratch / "progress.out").write_text("step-1", encoding="utf-8")
                dest = workdir / "progress.out"
                deadline = time.time() + 5
                while time.time() < deadline:
                    if dest.exists() and dest.read_text(encoding="utf-8") == "step-1":
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("watchdog did not copy progress.out")
                (scratch / "progress.out").write_text("step-2", encoding="utf-8")
                deadline = time.time() + 5
                while time.time() < deadline:
                    if dest.read_text(encoding="utf-8") == "step-2":
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("watchdog did not update progress.out")
            finally:
                runner.retrieve(output_files=[])
            assert runner._file_watchdog_proc is None
        finally:
            os.chdir(original_cwd)

    def test_retrieve_stops_watchdog_process(self, tmp_path, monkeypatch):
        runner, workdir = _staged_runner(tmp_path, monkeypatch)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner.stage()
            runner.file_watchdog(["progress.out"], interval=0.2)
            proc = runner._file_watchdog_proc
            assert proc is not None
            assert proc.poll() is None
            runner.retrieve(output_files=[])
            assert proc.poll() is not None
        finally:
            os.chdir(original_cwd)

    def test_fail_stops_watchdog_process(self, tmp_path, monkeypatch):
        runner, workdir = _staged_runner(tmp_path, monkeypatch)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner.stage()
            runner.file_watchdog(["progress.out"], interval=0.2)
            proc = runner._file_watchdog_proc
            runner.fail("boom")
            assert proc.poll() is not None
        finally:
            os.chdir(original_cwd)

    def test_file_watchdog_requires_stage(self):
        runner = NodeRunner("orca", "watch_task")
        with pytest.raises(ValueError, match="stage"):
            runner.file_watchdog(["a.out"], interval=1)

    def test_file_watchdog_rejects_empty_files(self, tmp_path, monkeypatch):
        runner, workdir = _staged_runner(tmp_path, monkeypatch)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner.stage()
            with pytest.raises(ValueError, match="non-empty"):
                runner.file_watchdog([], interval=1)
        finally:
            os.chdir(original_cwd)

    def test_file_watchdog_rejects_bad_interval(self, tmp_path, monkeypatch):
        runner, workdir = _staged_runner(tmp_path, monkeypatch)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner.stage()
            with pytest.raises(ValueError, match="interval"):
                runner.file_watchdog(["a.out"], interval=0)
            with pytest.raises(ValueError, match="interval"):
                runner.file_watchdog(["a.out"], interval=-1)
            with pytest.raises(ValueError, match="interval"):
                runner.file_watchdog(["a.out"], interval=True)
        finally:
            os.chdir(original_cwd)
