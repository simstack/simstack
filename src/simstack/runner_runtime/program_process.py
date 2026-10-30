"""Trusted Linux lifecycle wrapper for one named-program invocation.

This wrapper is a child subreaper, not a task scheduler. It stays in the task's
allocation and owns only its descendant tree. Native children that create a new
process group or become orphaned are still cleaned without touching sibling
program invocations in the same worker session. The file uses only stdlib so it
can be launched with the worker interpreter's isolated mode.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _processes() -> dict[int, tuple[int, int]]:
    result = {}
    for item in Path("/proc").iterdir():
        if not item.name.isdecimal():
            continue
        try:
            value = (item / "stat").read_text().rpartition(") ")[2].split()
            # The fields after comm start at field 3 (state); ppid is 4,
            # starttime is 22. Birth identity prevents acting on reused PIDs.
            if len(value) >= 20:
                result[int(item.name)] = (int(value[1]), int(value[19]))
        except (OSError, ValueError):
            continue
    return result


def _descendants(root: int) -> dict[int, int]:
    processes = _processes()
    found, pending = {}, [root]
    while pending:
        parent = pending.pop()
        for pid, (ppid, birth) in processes.items():
            if ppid == parent and pid != root and pid not in found:
                found[pid] = birth
                pending.append(pid)
    return found


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _signal_process(pid: int, birth: int, sig: signal.Signals) -> None:
    try:
        descriptor = os.pidfd_open(pid) if hasattr(os, "pidfd_open") else None
    except OSError:
        descriptor = None
    try:
        try:
            values = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
            current_birth = int(values[19]) if len(values) >= 20 else None
        except (OSError, ValueError):
            return
        if current_birth != birth:
            return
        if descriptor is not None and hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(descriptor, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cleanup(root: int) -> None:
    started = time.monotonic()
    while time.monotonic() - started < 3:
        _reap()
        children = _descendants(root)
        if not children:
            return
        sig = signal.SIGTERM if time.monotonic() - started < 0.1 else signal.SIGKILL
        for pid, birth in reversed(tuple(children.items())):
            _signal_process(pid, birth, sig)
        time.sleep(0.02)
    _reap()
    if _descendants(root):
        raise RuntimeError("Named-program descendants did not terminate")


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("Named-program lifecycle wrapper requires an executable")
    if sys.platform != "linux":
        raise RuntimeError("Named-program lifecycle wrapper requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot establish program child ownership")
    stopped = 0

    def stop(signum, _frame):
        nonlocal stopped
        stopped = signum

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = subprocess.Popen(command)
    try:
        while process.poll() is None and not stopped:
            time.sleep(0.02)
        result = process.returncode if not stopped else 128 + stopped
    finally:
        _cleanup(os.getpid())
        process.poll()
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
