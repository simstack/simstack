"""Submit bounded runner work to the file-queue watchdog."""

import json
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

POLL_INTERVAL = 1.0
TIMEOUT_SEC = 60 * 60 * 72  # adjust


class WatchdogResult(BaseModel):
    job_id: str
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    paths: dict[str, str]


def submit_to_watchdog(
    command: Sequence[str],
    job_id: str,
    queue_dir: Path = Path.cwd() / "queue",
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> WatchdogResult:
    """
    Submit a command to be executed by the file-queue watcher.
    Returns a WatchdogResult with status, exit_code, stdout, stderr, and paths.
    """
    if job_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", job_id):
        raise ValueError("Watchdog job_id must be a strict path component")
    if (
        isinstance(command, (str, bytes))
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValueError("Watchdog command must be a non-empty argv sequence")
    if (
        not isinstance(env, dict)
        or not env
        or not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in env.items()
        )
    ):
        raise ValueError("Watchdog requires a complete explicit environment")
    queue_dir = queue_dir.resolve()
    base = queue_dir / job_id
    cmd_file_tmp = base.with_suffix(".cmd.tmp")
    cmd_file = base.with_suffix(".cmd")
    ready_file = base.with_suffix(".ready")
    done_file = base.with_suffix(".done")
    fail_file = base.with_suffix(".fail")
    exit_file = base.with_suffix(".exit")
    out_file = base.with_suffix(".stdout")
    err_file = base.with_suffix(".stderr")
    for path in (cmd_file_tmp, cmd_file, ready_file, done_file, fail_file, exit_file, out_file, err_file):
        if path.resolve().parent != queue_dir:
            raise ValueError("Watchdog job files must remain inside the queue directory")

    data = json.dumps(
        {
            "argv": list(command),
            "cwd": cwd,
            "env": env,
            "timeout": TIMEOUT_SEC,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    queue_dir.chmod(0o700)
    fd = os.open(cmd_file_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(cmd_file_tmp, cmd_file)  # atomic
    cmd_file.chmod(0o600)
    ready_fd = os.open(ready_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(ready_fd)

    deadline = time.time() + TIMEOUT_SEC
    while time.time() < deadline:
        if done_file.exists() or fail_file.exists():
            break
        time.sleep(POLL_INTERVAL)

    result = WatchdogResult(
        job_id=job_id,
        status="timeout",
        returncode=None,
        stdout="",
        stderr="",
        paths={
            "cmd": str(cmd_file),
            "ready": str(ready_file),
            "done": str(done_file),
            "fail": str(fail_file),
            "exit": str(exit_file),
            "stdout": str(out_file),
            "stderr": str(err_file),
        },
    )

    try:
        if done_file.exists() or fail_file.exists():
            exit_code: int | None = None
            if exit_file.exists():
                try:
                    exit_code = int(exit_file.read_text().strip())
                except Exception:
                    exit_code = None
            out = out_file.read_text(errors="replace") if out_file.exists() else ""
            err = err_file.read_text(errors="replace") if err_file.exists() else ""
            ok = done_file.exists() and (exit_code == 0)

            result.status = "ok" if ok else "failed"
            result.returncode = exit_code
            result.stdout = out
            result.stderr = err
        return result
    finally:
        for artifact in (
            cmd_file_tmp,
            cmd_file,
            ready_file,
            done_file,
            fail_file,
            exit_file,
            out_file,
            err_file,
        ):
            artifact.unlink(missing_ok=True)


if __name__ == "__main__":
    res = submit_to_watchdog(["dir"], uuid4().hex, env={"PATH": os.environ["PATH"]})
    print(res)
