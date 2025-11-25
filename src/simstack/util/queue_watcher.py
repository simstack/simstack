#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, Tuple


def parse_spec(
    cmd_file: Path
) -> Tuple[Optional[str], Dict[str, str], Optional[str], Optional[int]]:
    cwd: Optional[str] = None
    env: Dict[str, str] = {}
    command: Optional[str] = None
    timeout: Optional[int] = None

    with cmd_file.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("CWD="):
                cwd = line[len("CWD=") :]
            elif line.startswith("ENV[") and "]=" in line:
                key = line[len("ENV[") : line.index("]=", len("ENV["))]
                val = line.split("]=", 1)[1]
                env[key] = val
            elif line.startswith("CMD="):
                command = line[len("CMD=") :]
            elif line.startswith("TIMEOUT="):
                try:
                    timeout = int(line[len("TIMEOUT=") :])
                except ValueError:
                    timeout = None
    return cwd, env, command, timeout


def run_command(
    command: str,
    cwd: Optional[str],
    env: Dict[str, str],
    timeout: Optional[int],
    out_file: Path,
    err_file: Path,
) -> int:
    proc_env = os.environ.copy()
    proc_env.update(env)
    with out_file.open("w", encoding="utf-8") as out, err_file.open(
        "w", encoding="utf-8"
    ) as err:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd if cwd else None,
                env=proc_env,
                stdout=out,
                stderr=err,
                text=True,
                timeout=timeout if (timeout and timeout > 0) else None,
                check=False,
            )
            return proc.returncode
        except subprocess.TimeoutExpired:
            err.write("\n[watcher] Command timed out\n")
            return 124
        except Exception as e:
            err.write(f"\n[watcher] Execution error: {e}\n")
            return 1


def process_job_from_claim(base_stem: Path, claimed_by_lock: bool) -> None:
    """
    Process a job using base path (without suffix). If claimed_by_lock is True, we did not
    rename .ready; we will remove it at the end. Otherwise, we operate on .claim and remove it.
    """
    cmd_file = base_stem.with_suffix(".cmd")
    ready_file = base_stem.with_suffix(".ready")
    claim_file = base_stem.with_suffix(".claim")
    lock_file = base_stem.with_suffix(".lock")
    out_file = base_stem.with_suffix(".stdout")
    err_file = base_stem.with_suffix(".stderr")
    exit_file = base_stem.with_suffix(".exit")
    done_file = base_stem.with_suffix(".done")
    fail_file = base_stem.with_suffix(".fail")

    try:
        if not cmd_file.exists():
            fail_file.touch()
            return

        cwd, env, command, timeout = parse_spec(cmd_file)
        if not command:
            err_file.write_text("[watcher] CMD missing in spec\n", encoding="utf-8")
            exit_file.write_text("2\n", encoding="utf-8")
            fail_file.touch()
            return

        rc = run_command(command, cwd, env, timeout, out_file, err_file)
        print(f"[watcher] Job {base_stem.name} finished with rc={rc}")
        exit_file.write_text(f"{rc}\n", encoding="utf-8")
        print(f"[watcher] Job {base_stem.name} finished")
        (done_file if rc == 0 else fail_file).touch()
    finally:
        # Cleanup markers depending on the claim type
        if claimed_by_lock:
            try:
                if ready_file.exists():
                    ready_file.unlink()
            except Exception as e:
                print(
                    f"[watcher] Warning: failed to remove ready {ready_file}: {e}",
                    file=sys.stderr,
                )
            try:
                if lock_file.exists():
                    lock_file.unlink()
            except Exception as e:
                print(
                    f"[watcher] Warning: failed to remove lock {lock_file}: {e}",
                    file=sys.stderr,
                )
        else:
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


def try_lock(base_stem: Path) -> bool:
    """
    Create an exclusive lock file <id>.lock using O_CREAT|O_EXCL.
    Returns True if lock acquired, False otherwise.
    """
    lock_path = base_stem.with_suffix(".lock")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock_path, flags, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        print(f"[watcher] Lock create failed for {lock_path}: {e}", file=sys.stderr)
        return False


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
        "--use-srun",
        action="store_true",
        help="(deprecated; wrapper removed for simplicity)",
    )
    parser.add_argument("--srun-args", type=str, default="")
    parser.add_argument(
        "--once", action="store_true", help="Process current claims and exit"
    )
    args = parser.parse_args()

    qd = args.queue_dir
    qd.mkdir(parents=True, exist_ok=True)
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
                    futures.add(pool.submit(process_job_from_claim, base_stem, False))
                    continue

                # 2) Fallback: exclusive lock file if rename failed
                if try_lock(base_stem):
                    print(f"[watcher] Claimed by lock: {base_stem.name}")
                    claimed_any = True
                    futures.add(pool.submit(process_job_from_claim, base_stem, True))

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
