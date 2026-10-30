"""Child process that copies listed scratch files into the node directory.

Spawned by ``NodeRunner.file_watchdog``. Runs in a separate interpreter so it
does not hold the GIL of the parent. Exits when the parent PID disappears
(and on Linux is also sent SIGTERM via ``PR_SET_PDEATHSIG``).
"""

import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if not ok:
            return False
        return exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def copy_listed_files(scratch: Path, dest: Path, files: list[str]) -> None:
    for name in files:
        src = scratch / name
        if not src.is_file():
            continue
        dest_path = dest / name
        if src.resolve() == dest_path.resolve():
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(dest_path.name + ".watchdog.tmp")
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dest_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scratch_file_watchdog")
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        raise SystemExit(f"interval must be a positive number, got {args.interval!r}")

    if sys.platform == "linux":
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        if not pid_is_alive(args.parent_pid):
            os._exit(0)

    scratch = Path(args.scratch)
    dest = Path(args.dest)
    while True:
        if not pid_is_alive(args.parent_pid):
            os._exit(0)
        copy_listed_files(scratch, dest, args.files)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
