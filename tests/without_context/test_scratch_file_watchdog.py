import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from simstack.util.scratch_file_watchdog import copy_listed_files, pid_is_alive


def test_pid_is_alive_for_current_process():
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_alive_for_dead_process():
    dead = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    dead.wait(timeout=5)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not pid_is_alive(dead.pid):
            break
        time.sleep(0.05)
    assert pid_is_alive(dead.pid) is False


def test_copy_listed_files_atomic(tmp_path):
    scratch = tmp_path / "scratch"
    dest = tmp_path / "dest"
    scratch.mkdir()
    dest.mkdir()
    (scratch / "a.txt").write_text("hello", encoding="utf-8")
    (scratch / "skip.txt").write_text("nope", encoding="utf-8")
    copy_listed_files(scratch, dest, ["a.txt", "missing.txt"])
    assert (dest / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not (dest / "skip.txt").exists()
    assert not (dest / "missing.txt").exists()


def test_watchdog_exits_when_parent_pid_is_dead(tmp_path):
    scratch = tmp_path / "scratch"
    dest = tmp_path / "dest"
    scratch.mkdir()
    dest.mkdir()
    (scratch / "a.txt").write_text("data", encoding="utf-8")
    dead = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    dead.wait(timeout=5)
    env = os.environ.copy()
    import simstack

    src_dir = str(Path(simstack.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_dir if not existing else src_dir + os.pathsep + existing
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "simstack.util.scratch_file_watchdog",
            "--scratch",
            str(scratch),
            "--dest",
            str(dest),
            "--interval",
            "0.1",
            "--parent-pid",
            str(dead.pid),
            "a.txt",
        ],
        env=env,
    )
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        pytest.fail("watchdog child did not exit after parent pid died")
    assert child.returncode == 0
