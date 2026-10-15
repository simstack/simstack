"""Bounded, cancellable operator subprocesses for workflow installation.

This module is also the trusted POSIX rlimit launcher. It is started with
``-I -S`` by the operator interpreter, before any package interpreter or
operator sandbox. It must therefore keep its top-level imports stdlib-only.
Resource limits supplement, and never replace, the configured sandbox.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Iterator, Mapping, Sequence

if TYPE_CHECKING:
    from simstack.workflows.limits import WorkflowInstallationLimits


class WorkflowProcessLimitError(RuntimeError):
    """An installer process exceeded an operator resource budget."""


class _ProcessCancelled(Exception):
    pass


def _check_interrupt(deadline: float, cancelled: threading.Event, command) -> None:
    if cancelled.is_set():
        raise _ProcessCancelled
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(command, 0)


def check_storage_budget(root: Path, max_bytes: int, max_files: int | None = None) -> None:
    """Count actual service-owned regular bytes without following symlinks."""
    from simstack.workflows.limits import DEFAULT_INSTALLATION_LIMITS

    if max_files is None:
        max_files = DEFAULT_INSTALLATION_LIMITS.max_storage_files
    if max_bytes <= 0 or max_files <= 0:
        raise ValueError("Workflow storage budgets must be positive")
    if (
        not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd
    ):
        raise WorkflowProcessLimitError("Workflow storage scanning requires no-follow directory handles")
    total = entries_seen = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)  # Missing/inaccessible roots are never ignored.
    try:
        entries = os.scandir(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    pending = [(descriptor, entries)]
    try:
        while pending:
            descriptor, entries = pending[-1]
            try:
                entry = next(entries)
            except StopIteration:
                pending.pop()
                entries.close()
                os.close(descriptor)
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                # pip cleanup and atomic publication can remove an entry.
                continue
            entries_seen += 1
            if entries_seen > max_files:
                raise WorkflowProcessLimitError("Workflow installation storage entry budget exceeded")
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child = os.open(entry.name, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                try:
                    child_entries = os.scandir(child)
                except FileNotFoundError:
                    os.close(child)
                    continue
                except BaseException:
                    os.close(child)
                    raise
                # DFS keeps handles only for the current depth. A renamed
                # ancestor cannot redirect a queued child scan outside root.
                pending.append((child, child_entries))
            elif stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
                if total > max_bytes:
                    raise WorkflowProcessLimitError("Workflow installation storage budget exceeded")
    finally:
        for descriptor, entries in pending:
            entries.close()
            os.close(descriptor)


@contextmanager
def _process_slot(
    root: Path | None,
    maximum: int,
    deadline: float,
    cancelled: threading.Event,
    command,
) -> Iterator[None]:
    if root is None:
        # Callers executing a package must provide its operator-owned storage
        # root. Small standalone helpers can use an in-process-only semaphore.
        with _local_slots_lock:
            semaphore = _local_slots.setdefault(maximum, threading.BoundedSemaphore(maximum))
        while not semaphore.acquire(timeout=0.05):
            _check_interrupt(deadline, cancelled, command)
        try:
            yield
        finally:
            semaphore.release()
        return
    try:
        import fcntl
    except ImportError as exc:
        raise WorkflowProcessLimitError(
            "Workflow installation requires POSIX process-slot locking"
        ) from exc
    slots = root / ".process-slots"
    slots.mkdir(mode=0o700, exist_ok=True)
    if slots.is_symlink():
        raise WorkflowProcessLimitError("Workflow process-slot directory is invalid")
    descriptor = None
    try:
        while descriptor is None:
            _check_interrupt(deadline, cancelled, command)
            for index in range(maximum):
                candidate = os.open(
                    slots / f"{index}.lock",
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(candidate)
                    continue
                descriptor = candidate
                break
            if descriptor is None:
                cancelled.wait(0.05)
        yield
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


_local_slots_lock = threading.Lock()
_local_slots: dict[int, threading.BoundedSemaphore] = {}


class _AsyncProcessAdmission:
    def __init__(self, maximum: int):
        self.semaphore = asyncio.BoundedSemaphore(maximum)
        self.users = 0


_async_slots: dict[
    tuple[asyncio.AbstractEventLoop, Path | None, int], _AsyncProcessAdmission
] = {}


def _mount_path(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _current_cgroup(
    *, membership: Path = Path("/proc/self/cgroup"),
    mounts: Path = Path("/proc/self/mountinfo"),
) -> tuple[Path, Path]:
    """Resolve the kernel's current unified cgroup, including mounted subtrees."""
    current = None
    for line in membership.read_text().splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers:
            current = PurePosixPath(path)
            break
    if current is None or not current.is_absolute() or ".." in current.parts:
        raise ValueError("No unified cgroup-v2 membership")
    for line in mounts.read_text().splitlines():
        before, separator, after = line.partition(" - ")
        if not separator or after.split()[0] != "cgroup2":
            continue
        fields = before.split()
        root = PurePosixPath(_mount_path(fields[3]))
        mount = Path(_mount_path(fields[4]))
        if not root.is_absolute() or not mount.is_absolute() or ".." in root.parts:
            raise ValueError("Invalid cgroup-v2 mount")
        if current.is_relative_to(root):
            return mount, mount / current.relative_to(root)
    raise ValueError("Current cgroup-v2 hierarchy is not visible")


def _cgroup_resource_value(directory: Path, name: str) -> list[str] | None:
    try:
        return (directory / name).read_text().split()
    except FileNotFoundError:
        # The hierarchy root does not expose every controller resource file.
        return None


def _check_aggregate_budgets(max_memory_bytes: int, max_cpu_cores: int) -> None:
    """Prove finite *kernel* ancestor ceilings, not a process-count estimate.

    Children inherit these shared service/container ceilings; this is not a
    separately delegated per-installation cgroup. No cgroups are mutated here.
    """
    try:
        mount, current = _current_cgroup()
        memory_ok = cpu_ok = False
        while True:
            memory = _cgroup_resource_value(current, "memory.max")
            if memory is not None:
                if len(memory) != 1:
                    raise ValueError("Invalid cgroup-v2 memory ceiling")
                if memory[0] != "max":
                    ceiling = int(memory[0])
                    if ceiling <= 0:
                        raise ValueError("Invalid cgroup-v2 memory ceiling")
                    memory_ok |= ceiling <= max_memory_bytes
            cpu = _cgroup_resource_value(current, "cpu.max")
            if cpu is not None:
                if len(cpu) != 2 or int(cpu[1]) <= 0:
                    raise ValueError("Invalid cgroup-v2 CPU ceiling")
                if cpu[0] != "max":
                    quota, period = int(cpu[0]), int(cpu[1])
                    if quota <= 0:
                        raise ValueError("Invalid cgroup-v2 CPU ceiling")
                    cpu_ok |= quota <= max_cpu_cores * period
            if memory_ok and cpu_ok:
                return
            if current == mount:
                break
            current = current.parent
        if not memory_ok:
            raise ValueError("No finite aggregate memory ceiling within operator budget")
        raise ValueError("No finite aggregate CPU ceiling within operator budget")
    except (OSError, ValueError, IndexError) as exc:
        raise WorkflowProcessLimitError(
            "Workflow installation requires finite cgroup-v2 memory and CPU quotas "
            "within the configured process-tree budgets"
        ) from exc


def _limited_command(command: Sequence[str], limits: WorkflowInstallationLimits) -> list[str]:
    if os.name != "posix":
        raise WorkflowProcessLimitError("Workflow installation requires POSIX resource limits")
    if sys.platform != "linux":
        raise WorkflowProcessLimitError(
            "Workflow installation requires a supported Linux resource-limit runtime"
        )
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        raise WorkflowProcessLimitError(
            "Workflow installation requires an unprivileged service UID: "
            "Linux root is exempt from the process-count resource limit"
        )
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("Workflow process command must be a nonempty argv")
    _check_aggregate_budgets(
        limits.max_process_tree_memory_bytes, limits.max_process_tree_cpu_cores
    )
    return [
        sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--resource-limits",
        str(limits.max_process_memory_bytes), str(limits.max_process_cpu_seconds),
        str(limits.max_process_file_bytes), str(limits.max_processes),
        str(limits.max_process_tree_memory_bytes), str(limits.max_process_tree_cpu_cores),
        "--", *command,
    ]


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_process(
    command: Sequence[str],
    *,
    input_data: bytes | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
    limits: WorkflowInstallationLimits | None = None,
    storage_root: Path | None = None,
    slot_root: Path | None = None,
    check: bool = False,
    _cancelled: threading.Event | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Drain both pipes concurrently, failing before retaining over-budget output."""
    from simstack.workflows.limits import WorkflowInstallationLimits

    limits = limits or WorkflowInstallationLimits()
    if timeout <= 0:
        raise ValueError("Workflow process timeout must be positive")
    cancelled = _cancelled or threading.Event()
    deadline = time.monotonic() + timeout
    root = Path(storage_root).resolve(strict=True) if storage_root is not None else None
    slots = Path(slot_root).resolve(strict=True) if slot_root is not None else root
    limited_command = _limited_command(command, limits)
    with _process_slot(slots, limits.max_parallel_processes, deadline, cancelled, command):
        _check_interrupt(deadline, cancelled, command)
        if root is not None:
            check_storage_budget(root, limits.max_storage_bytes, limits.max_storage_files)
        process = subprocess.Popen(
            limited_command,
            cwd=cwd, env=env,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = bytearray(), bytearray()
        output_bytes, input_offset = 0, 0
        monitor_at = time.monotonic()
        try:
            with selectors.DefaultSelector() as streams:
                for stream, target in ((process.stdout, stdout), (process.stderr, stderr)):
                    os.set_blocking(stream.fileno(), False)
                    streams.register(stream, selectors.EVENT_READ, target)
                if process.stdin is not None:
                    if input_data:
                        os.set_blocking(process.stdin.fileno(), False)
                        streams.register(process.stdin, selectors.EVENT_WRITE, None)
                    else:
                        process.stdin.close()
                while streams.get_map():
                    _check_interrupt(deadline, cancelled, command)
                    if root is not None and time.monotonic() >= monitor_at:
                        check_storage_budget(root, limits.max_storage_bytes, limits.max_storage_files)
                        monitor_at = time.monotonic() + 0.2
                    for key, _ in streams.select(timeout=min(0.05, max(0, deadline - time.monotonic()))):
                        if key.events == selectors.EVENT_WRITE:
                            try:
                                input_offset += os.write(key.fd, input_data[input_offset:input_offset + 65536])
                            except BrokenPipeError:
                                input_offset = len(input_data)
                            if input_offset == len(input_data):
                                streams.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        block = os.read(key.fd, min(65536, limits.max_output_bytes - output_bytes + 1))
                        if not block:
                            streams.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        output_bytes += len(block)
                        if output_bytes > limits.max_output_bytes:
                            raise WorkflowProcessLimitError("Workflow process output budget exceeded")
                        key.data.extend(block)
                while process.poll() is None:
                    _check_interrupt(deadline, cancelled, command)
                    if root is not None:
                        check_storage_budget(root, limits.max_storage_bytes, limits.max_storage_files)
                    cancelled.wait(0.05)
            if root is not None:
                check_storage_budget(root, limits.max_storage_bytes, limits.max_storage_files)
        except BaseException as exc:
            _kill_group(process)
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.timeout = timeout
            raise
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        # A launcher may finish while leaving background descendants. They are
        # not allowed to outlive this bounded invocation, including on success.
        _kill_group(process)
        result = subprocess.CompletedProcess(command, process.returncode, bytes(stdout), bytes(stderr))
        if check:
            result.check_returncode()
        return result


async def run_process_async(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
    """Queue without executor threads; cancellation still waits for child cleanup.

    This admission gate complements, not replaces, run_process's interprocess
    flock. At most the configured number of calls in this event loop can occupy
    executor threads while competing for the shared kernel-backed slots.
    """
    from simstack.workflows.limits import WorkflowInstallationLimits

    limits = kwargs.get("limits") or WorkflowInstallationLimits()
    timeout = kwargs.get("timeout", 600)
    if timeout <= 0:
        raise ValueError("Workflow process timeout must be positive")
    deadline = time.monotonic() + timeout
    slot_root = kwargs.get("slot_root")
    if slot_root is None:
        slot_root = kwargs.get("storage_root")
    root = Path(slot_root).resolve(strict=True) if slot_root is not None else None
    key = (asyncio.get_running_loop(), root, limits.max_parallel_processes)
    admission = _async_slots.get(key)
    if admission is None:
        admission = _async_slots[key] = _AsyncProcessAdmission(limits.max_parallel_processes)
    admission.users += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(admission.semaphore.acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise subprocess.TimeoutExpired(command, timeout) from exc
        acquired = True
        cancelled = threading.Event()

        def invoke():
            _check_interrupt(deadline, cancelled, command)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            return run_process(
                command, _cancelled=cancelled, **{**kwargs, "timeout": remaining}
            )

        work = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled.set()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Repeated cancellation must not return while the child lives.
                    continue
                except Exception:
                    break
            if not work.cancelled():
                try:
                    work.result()
                except Exception:
                    pass
            raise
    except subprocess.TimeoutExpired as exc:
        # Queueing and execution share the caller's one original timeout budget.
        exc.timeout = timeout
        raise
    finally:
        if acquired:
            admission.semaphore.release()
        admission.users -= 1
        if not admission.users:
            _async_slots.pop(key, None)


def _exec_resource_limits(arguments: list[str]) -> None:
    """Apply limits in a fresh trusted process, not threaded ``preexec_fn``."""
    import resource

    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError("unsupported resource-limit runtime")
        if len(arguments) < 8 or arguments[6] != "--":
            raise ValueError("invalid resource launcher arguments")
        values = [int(value) for value in arguments[:6]]
        if any(value <= 0 for value in values):
            raise ValueError("resource limits must be positive")
        _check_aggregate_budgets(values[4], values[5])
        names = ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC")
        for name, requested in zip(names, values[:4], strict=True):
            identifier = getattr(resource, name)
            _, hard = resource.getrlimit(identifier)
            value = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            resource.setrlimit(identifier, (value, value))
        os.execvp(arguments[7], arguments[7:])
    except (OSError, ValueError, AttributeError, WorkflowProcessLimitError):
        # Do not expose argv, environment, paths or OS diagnostics to tenants.
        sys.stderr.write("Workflow process resource boundary could not be established\n")
        raise SystemExit(125)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "--resource-limits":
        raise SystemExit(125)
    _exec_resource_limits(sys.argv[2:])
