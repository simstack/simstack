#!/usr/bin/env python3
"""Fail-closed file-queue watchdog worker."""

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, Tuple


def _private_open_flags(*, exclusive: bool = False) -> int:
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _write_private(path: Path, value: str) -> None:
    fd = os.open(path, _private_open_flags(exclusive=True), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value)
    finally:
        if fd >= 0:
            os.close(fd)


def _touch_private(path: Path) -> None:
    _write_private(path, "")


def parse_spec(
    cmd_file: Path,
) -> Tuple[Optional[str], Dict[str, str], list[str], Optional[int]]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(cmd_file, flags)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("Watchdog spec must be a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                payload = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Watchdog spec must be valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "argv",
        "cwd",
        "env",
        "timeout",
    }:
        raise ValueError("Watchdog spec has an invalid shape")
    argv = payload["argv"]
    env = payload["env"]
    cwd = payload["cwd"]
    timeout = payload["timeout"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ValueError("Watchdog argv must be a non-empty list")
    if (
        not isinstance(env, dict)
        or not env
        or not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in env.items()
        )
    ):
        raise ValueError("Watchdog environment must be explicit and non-empty")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("Watchdog cwd must be a string or null")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        raise ValueError("Watchdog timeout must be a positive integer or null")
    return cwd, env, argv, timeout


def run_command(
    command: list[str],
    network_guard_command: list[str],
    cwd: Optional[str],
    env: Dict[str, str],
    timeout: Optional[int],
    out_file: Path,
    err_file: Path,
) -> int:
    # An explicit environment is a complete, sanitized runner contract.  Never
    # merge it with the watcher's potentially credential-bearing environment.
    if not env:
        raise ValueError("Watchdog environment cannot be empty")
    if not network_guard_command or not all(
        isinstance(item, str) and item for item in network_guard_command
    ):
        raise ValueError("Watchdog network guard command must be a non-empty argv")
    proc_env = dict(env)
    out_fd = os.open(out_file, _private_open_flags(exclusive=True), 0o600)
    err_fd = os.open(err_file, _private_open_flags(exclusive=True), 0o600)
    with (
        os.fdopen(out_fd, "w", encoding="utf-8") as out,
        os.fdopen(err_fd, "w", encoding="utf-8") as err,
    ):
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                [*network_guard_command, *command],
                shell=False,
                cwd=cwd if cwd else None,
                env=proc_env,
                stdout=out,
                stderr=err,
                text=True,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            err.write("\n[watcher] Command timed out\n")
            return 124
        except Exception as e:
            err.write(f"\n[watcher] Execution error: {e}\n")
            return 1


def _terminate_process_tree(
    process: subprocess.Popen[object], *, timeout_seconds: float = 5.0
) -> None:
    if process.poll() is not None:
        return
    _signal_process_tree(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process_tree(process, signal.SIGKILL)
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Watchdog process {process.pid} did not exit after SIGKILL")


def _signal_process_tree(
    process: subprocess.Popen[object], requested_signal: signal.Signals
) -> None:
    if os.name != "nt" and process.pid is not None:
        try:
            os.killpg(process.pid, requested_signal)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        if requested_signal is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return


def process_job_from_claim(base_stem: Path, network_guard_command: list[str]) -> None:
    """
    Process a job using base path (without suffix). If claimed_by_lock is True, we did not
    rename .ready; we will remove it at the end. Otherwise, we operate on .claim and remove it.
    """
    cmd_file = base_stem.with_suffix(".cmd")
    claim_file = base_stem.with_suffix(".claim")
    out_file = base_stem.with_suffix(".stdout")
    err_file = base_stem.with_suffix(".stderr")
    exit_file = base_stem.with_suffix(".exit")
    done_file = base_stem.with_suffix(".done")
    fail_file = base_stem.with_suffix(".fail")

    try:
        if not cmd_file.exists():
            _touch_private(fail_file)
            return

        try:
            try:
                cwd, env, command, timeout = parse_spec(cmd_file)
            except Exception as exc:
                _write_private(
                    err_file,
                    f"[watcher] Invalid job specification: {exc}\n",
                )
                _write_private(exit_file, "1\n")
                _touch_private(fail_file)
                return
        finally:
            cmd_file.unlink(missing_ok=True)

        rc = run_command(
            command,
            network_guard_command,
            cwd,
            env,
            timeout,
            out_file,
            err_file,
        )
        print(f"[watcher] Job {base_stem.name} finished with rc={rc}")
        _write_private(exit_file, f"{rc}\n")
        print(f"[watcher] Job {base_stem.name} finished")
        _touch_private(done_file if rc == 0 else fail_file)
    finally:
        cmd_file.unlink(missing_ok=True)
        try:
            if claim_file.exists():
                claim_file.unlink()
        except Exception as e:
            print(
                f"[watcher] Warning: failed to remove claim {claim_file}: {e}",
                file=sys.stderr,
            )


def try_claim(
    ready_file: Path, retries: int = 5, backoff: float = 0.05
) -> Optional[Path]:
    """
    Atomically rename *.ready -> *.claim. Retries on transient failures.
    Returns claim path if successful, else None.
    """
    claim = ready_file.with_suffix(".claim")
    for i in range(max(1, retries)):
        try:
            os.replace(ready_file, claim)
            return claim
        except FileNotFoundError:
            return None
        except PermissionError:
            # Windows/NFS transient locks: backoff and retry
            time.sleep(backoff * (i + 1))
        except OSError:
            # Other transient FS issues: backoff and retry
            time.sleep(backoff * (i + 1))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Concurrent file-queue watcher with robust claiming"
    )
    parser.add_argument(
        "--queue-dir", type=Path, required=False, default=Path.cwd() / "queue"
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=min(4, (os.cpu_count() or 2)))
    parser.add_argument(
        "--once", action="store_true", help="Process current claims and exit"
    )
    parser.add_argument(
        "--network-guard-command-json",
        required=True,
        help="Trusted non-empty JSON argv prepended to every workflow command",
    )
    args = parser.parse_args()

    try:
        network_guard_command = json.loads(args.network_guard_command_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--network-guard-command-json must be valid JSON: {exc}")
    if (
        not isinstance(network_guard_command, list)
        or not network_guard_command
        or not all(isinstance(item, str) and item for item in network_guard_command)
    ):
        parser.error("--network-guard-command-json must encode a non-empty argv list")

    qd = args.queue_dir
    qd.mkdir(parents=True, exist_ok=True, mode=0o700)
    qd.chmod(0o700)
    print(f"[watcher] Watching {qd}, workers={args.workers}")

    stop = {"flag": False}

    def handle_sig(_s, _f):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = set()

        while not stop["flag"]:
            claimed_any = False

            # Scan for all .ready files
            ready_list = list(qd.glob("*.ready"))
            if ready_list:
                print(f"[watcher] Found {len(ready_list)} ready file(s)")
            for ready in ready_list:
                base_stem = ready.with_suffix("")  # drop .ready
                # 1) Preferred: atomic rename to .claim (with retries)
                claim = try_claim(ready)
                if claim is not None:
                    claimed_any = True
                    futures.add(
                        pool.submit(
                            process_job_from_claim,
                            base_stem,
                            list(network_guard_command),
                        )
                    )

            # Reap done tasks
            done_now = {f for f in futures if f.done()}
            for f in done_now:
                try:
                    f.result()
                except Exception as e:
                    print(f"[watcher] Job raised: {e}", file=sys.stderr)
                futures.discard(f)

            if args.once:
                # Drain: wait for all running tasks to finish
                for f in as_completed(list(futures)):
                    try:
                        f.result()
                    except Exception as e:
                        print(f"[watcher] Job raised: {e}", file=sys.stderr)
                break

            if not claimed_any:
                time.sleep(max(0.05, args.poll_interval))

    print("[watcher] Exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
