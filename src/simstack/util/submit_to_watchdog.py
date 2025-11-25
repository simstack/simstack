# python
import os
import time
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
    command: str,
    job_id: str,
    queue_dir: Path = Path.cwd() / "queue",
    env: dict | None = None,
    cwd: str | None = None,
) -> WatchdogResult:
    """
    Submit a command to be executed by the file-queue watcher.
    Returns a WatchdogResult with status, exit_code, stdout, stderr, and paths.
    """
    base = queue_dir / job_id
    cmd_file_tmp = base.with_suffix(".cmd.tmp")
    cmd_file = base.with_suffix(".cmd")
    ready_file = base.with_suffix(".ready")
    done_file = base.with_suffix(".done")
    fail_file = base.with_suffix(".fail")
    exit_file = base.with_suffix(".exit")
    out_file = base.with_suffix(".stdout")
    err_file = base.with_suffix(".stderr")

    spec_lines = []
    if cwd:
        spec_lines.append(f"CWD={cwd}")
    if env:
        for k, v in env.items():
            spec_lines.append(f"ENV[{k}]={v}")
    spec_lines.append("CMD=" + command)
    data = "\n".join(spec_lines) + "\n"

    queue_dir.mkdir(parents=True, exist_ok=True)
    print(f"[watcher] Submitting {data} to {cmd_file}")
    with open(cmd_file_tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(cmd_file_tmp, cmd_file)  # atomic
    ready_file.touch()  # signal

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


if __name__ == "__main__":
    res = submit_to_watchdog("dir", uuid4().hex)
    print(res)
