"""Tiny process-budget checks; no network, databases or uploaded code."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import asyncio
import errno
import os
import shutil
import subprocess
import sys
import threading

import pytest

from simstack.workflows.limits import WorkflowInstallationLimits
from simstack.workflows import processes


def _python(source: str) -> list[str]:
    return [sys.executable, "-I", "-S", "-c", source]


def _limits(**updates):
    return replace(WorkflowInstallationLimits(), **updates)


@pytest.mark.parametrize("cores,parallelism,threads", [(2, 2, 1), (8, 2, 4), (5, 2, 2), (1, 4, 1)])
def test_native_thread_policy_shares_operator_cpu_budget(cores, parallelism, threads):
    policy = _limits(max_process_tree_cpu_cores=cores, max_parallel_processes=parallelism).native_thread_environment()
    assert set(policy) == {
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "OMP_THREAD_LIMIT",
        "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS", "NUMEXPR_MAX_THREADS",
    }
    assert set(policy.values()) == {str(threads)}


@pytest.mark.parametrize("name", ["max_process_tree_cpu_cores", "max_parallel_processes"])
@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5])
def test_invalid_operator_native_thread_budgets_are_rejected(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        _limits(**{name: value})


@pytest.fixture(autouse=True)
def local_io_test_boundary(monkeypatch, request):
    """Test pipe handling on macOS; real rlimit checks also run under Linux."""
    if sys.platform != "darwin":
        return
    name = request.node.name
    if name in {
        "test_process_receives_resource_limits_before_command",
        "test_per_file_resource_limit_rejects_growth",
    }:
        pytest.skip("Actual POSIX resource enforcement requires supported Linux")
    if name not in {
        "test_linux_root_cannot_claim_process_count_enforcement",
        "test_non_posix_platform_fails_closed",
        "test_non_linux_resource_boundary_fails_closed",
    }:
        monkeypatch.setattr(processes, "_limited_command", lambda command, limits: list(command))


def test_process_preserves_input_both_streams_and_result(tmp_path):
    result = processes.run_process(
        _python("import sys; print(sys.stdin.read()); sys.stderr.write('diagnostic')"),
        input_data="привет".encode(), cwd=tmp_path, env={"PATH": os.defpath},
        timeout=5, storage_root=tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == "привет\n".encode()
    assert result.stderr == b"diagnostic"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_fails_not_truncates(tmp_path, stream):
    with pytest.raises(processes.WorkflowProcessLimitError, match="output budget"):
        processes.run_process(
            _python(f"import sys; sys.{stream}.write('x' * 1024)"),
            cwd=tmp_path, limits=_limits(max_output_bytes=512), timeout=5,
        )


def test_output_budget_counts_both_streams_together(tmp_path):
    with pytest.raises(processes.WorkflowProcessLimitError, match="output budget"):
        processes.run_process(
            _python("import sys; sys.stdout.write('x'*320); sys.stderr.write('y'*320)"),
            cwd=tmp_path, limits=_limits(max_output_bytes=512), timeout=5,
        )


def test_exact_output_limit_is_accepted(tmp_path):
    result = processes.run_process(
        _python("import sys; sys.stdout.write('x'*512)"), cwd=tmp_path,
        limits=_limits(max_output_bytes=512), timeout=5,
    )
    assert result.stdout == b"x" * 512


def test_writing_input_and_draining_both_outputs_do_not_deadlock(tmp_path):
    result = processes.run_process(
        _python(
            "import sys; sys.stdout.write('x'*16384); sys.stdout.flush(); "
            "sys.stderr.write('y'*16384); sys.stderr.flush(); "
            "print(len(sys.stdin.buffer.read()))"
        ),
        input_data=b"z" * 16384, cwd=tmp_path, timeout=5,
        limits=_limits(max_output_bytes=65536),
    )
    assert result.stdout == b"x" * 16384 + b"16384\n"
    assert result.stderr == b"y" * 16384


def test_nonzero_exit_and_check_preserve_called_process_error(tmp_path):
    command = _python("import sys; print('reply'); sys.stderr.write('private'); sys.exit(7)")
    result = processes.run_process(command, cwd=tmp_path, timeout=5)
    assert result.returncode == 7
    with pytest.raises(subprocess.CalledProcessError) as error:
        processes.run_process(command, cwd=tmp_path, timeout=5, check=True)
    assert error.value.returncode == 7
    assert error.value.output == b"reply\n"
    assert error.value.stderr == b"private"


def test_timeout_kills_and_waits_for_started_process(monkeypatch, tmp_path):
    started = []
    original = processes.subprocess.Popen
    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        processes.run_process(_python("import time; time.sleep(10)"), cwd=tmp_path, timeout=0.2)
    assert error.value.timeout == 0.2
    assert started[0].returncode is not None


@pytest.mark.asyncio
async def test_async_cancellation_kills_and_waits_for_started_process(monkeypatch, tmp_path):
    started = []
    original = processes.subprocess.Popen
    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    task = asyncio.create_task(processes.run_process_async(
        _python("import time; time.sleep(10)"), cwd=tmp_path, timeout=5,
    ))
    for _ in range(100):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started[0].returncode is not None


@pytest.mark.asyncio
async def test_repeated_async_cancellation_still_waits_for_cleanup(monkeypatch, tmp_path):
    finished = threading.Event()
    def cancellation_boundary(command, *, _cancelled, **kwargs):
        assert _cancelled.wait(2)
        finished.wait(0.1)
        finished.set()
        raise processes._ProcessCancelled
    monkeypatch.setattr(processes, "run_process", cancellation_boundary)
    task = asyncio.create_task(processes.run_process_async(_python("pass"), cwd=tmp_path))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_process_slot_timeout_releases_lock_for_next_invocation(monkeypatch, tmp_path):
    started = threading.Event()
    original = processes.subprocess.Popen
    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        started.set()
        return child
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    limits = _limits(max_parallel_processes=1)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(
            processes.run_process, _python("import time; time.sleep(0.3)"),
            cwd=tmp_path, storage_root=tmp_path, limits=limits, timeout=5,
        )
        assert started.wait(2)
        with pytest.raises(subprocess.TimeoutExpired):
            processes.run_process(
                _python("pass"), cwd=tmp_path, storage_root=tmp_path,
                limits=limits, timeout=0.05,
            )
        assert first.result(timeout=5).returncode == 0
    assert processes.run_process(
        _python("print('released')"), cwd=tmp_path, storage_root=tmp_path,
        limits=limits, timeout=5,
    ).stdout == b"released\n"


def test_storage_budget_checked_before_spawn(monkeypatch, tmp_path):
    (tmp_path / "existing").write_bytes(b"x" * 65)
    monkeypatch.setattr(processes.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(processes.WorkflowProcessLimitError, match="storage budget"):
        processes.run_process(
            _python("pass"), cwd=tmp_path, storage_root=tmp_path,
            limits=_limits(max_storage_bytes=64), timeout=5,
        )


def test_storage_growth_is_detected_and_process_stopped(tmp_path):
    with pytest.raises(processes.WorkflowProcessLimitError, match="storage budget"):
        processes.run_process(
            _python("import time; open('growth','wb').write(b'x'*96); time.sleep(5)"),
            cwd=tmp_path, storage_root=tmp_path,
            limits=_limits(max_storage_bytes=64), timeout=5,
        )


def test_storage_scan_does_not_follow_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "outside").write_bytes(b"x" * 100)
    (source / "link").symlink_to(tmp_path / "outside")
    processes.check_storage_budget(source, 1)


def _scan_hook(monkeypatch, name, replacement):
    """Wrappers below retain the original directory-handle API support."""
    capability = "supports_dir_fd" if name == "open" else "supports_fd"
    monkeypatch.setattr(processes.os, capability, getattr(processes.os, capability) | {replacement})
    monkeypatch.setattr(processes.os, name, replacement)


def test_storage_scan_accepts_descendant_removed_after_stat(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    child = root / "pip-metadata"
    child.mkdir(parents=True)
    (child / "temporary").write_bytes(b"temporary contents")
    (root / "kept").write_bytes(b"x")
    original = processes.os.open
    removed = []

    def racing_open(path, flags, *, dir_fd=None):
        if path == "pip-metadata" and dir_fd is not None:
            shutil.rmtree(child)
            removed.append(path)
        return original(path, flags, dir_fd=dir_fd)

    _scan_hook(monkeypatch, "open", racing_open)
    processes.check_storage_budget(root, 1)
    assert removed == ["pip-metadata"]


def test_storage_scan_accepts_missing_descendant_scandir_and_closes_handle(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    child = root / "pip-metadata"
    child.mkdir(parents=True)
    original_open, original_scan, original_close = processes.os.open, processes.os.scandir, processes.os.close
    child_handles, closed = [], []

    def open_directory(path, flags, *, dir_fd=None):
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        if path == "pip-metadata":
            child_handles.append(descriptor)
        return descriptor

    def missing_scan(descriptor):
        if descriptor in child_handles:
            raise FileNotFoundError("Descendant disappeared while pip cleaned up")
        return original_scan(descriptor)

    def close_directory(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    _scan_hook(monkeypatch, "open", open_directory)
    _scan_hook(monkeypatch, "scandir", missing_scan)
    monkeypatch.setattr(processes.os, "close", close_directory)
    processes.check_storage_budget(root, 1)
    assert len(child_handles) == 1 and child_handles[0] in closed


def test_storage_scan_missing_root_is_not_ignored(tmp_path):
    with pytest.raises(FileNotFoundError):
        processes.check_storage_budget(tmp_path / "missing-root", 1024)


@pytest.mark.parametrize("failure_point", ["root-open", "child-open", "root-scan", "child-scan"])
def test_storage_scan_permission_errors_propagate_without_leaking_handles(monkeypatch, tmp_path, failure_point):
    root = tmp_path / "owned"
    (root / "child").mkdir(parents=True)
    original_open, original_scan, original_close = processes.os.open, processes.os.scandir, processes.os.close
    handles = {}

    def open_directory(path, flags, *, dir_fd=None):
        name = "root" if dir_fd is None else "child"
        if failure_point == name + "-open":
            raise PermissionError("Denied directory access")
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        handles[descriptor] = name
        return descriptor

    def scan_directory(descriptor):
        if failure_point == handles[descriptor] + "-scan":
            raise PermissionError("Denied directory enumeration")
        return original_scan(descriptor)

    def close_directory(descriptor):
        handles.pop(descriptor)
        original_close(descriptor)

    _scan_hook(monkeypatch, "open", open_directory)
    _scan_hook(monkeypatch, "scandir", scan_directory)
    monkeypatch.setattr(processes.os, "close", close_directory)
    with pytest.raises(PermissionError):
        processes.check_storage_budget(root, 1024)
    assert handles == {}


def test_storage_scan_rejects_directory_swapped_to_symlink_after_stat(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"do not scan outside")
    original_open, original_scan = processes.os.open, processes.os.scandir
    scanned = []

    def swap_directory(path, flags, *, dir_fd=None):
        if path == "child" and dir_fd is not None:
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, dir_fd=dir_fd)

    def scan_directory(descriptor):
        scanned.append(os.fstat(descriptor).st_ino)
        return original_scan(descriptor)

    _scan_hook(monkeypatch, "open", swap_directory)
    _scan_hook(monkeypatch, "scandir", scan_directory)
    with pytest.raises(OSError) as changed:
        processes.check_storage_budget(root, 1)
    assert changed.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert outside.stat().st_ino not in scanned


def test_storage_scan_open_directory_cannot_be_redirected_by_ancestor_swap(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    child = root / "child"
    (child / "nested").mkdir(parents=True)
    (child / "nested" / "original").write_bytes(b"x")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "private").write_bytes(b"do not scan outside")
    retained = tmp_path / "retained-original"
    child_inode = child.stat().st_ino
    original_scan = processes.os.scandir
    scanned = []

    def swap_ancestor(descriptor):
        inode = os.fstat(descriptor).st_ino
        scanned.append(inode)
        if inode == child_inode:
            child.rename(retained)
            child.symlink_to(outside, target_is_directory=True)
        return original_scan(descriptor)

    _scan_hook(monkeypatch, "scandir", swap_ancestor)
    processes.check_storage_budget(root, 1)
    assert retained.exists() and child.is_symlink()
    assert outside.stat().st_ino not in scanned
    assert (outside / "nested").stat().st_ino not in scanned


def test_storage_scan_depth_first_handles_are_bounded_and_closed_on_budget_failure(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    for index in range(100):
        (root / str(index)).mkdir()
    deepest = root / "deep" / "child" / "leaf"
    deepest.mkdir(parents=True)
    (deepest / "large").write_bytes(b"too large")
    original_open, original_close = processes.os.open, processes.os.close
    handles, peak = set(), []

    def open_directory(path, flags, *, dir_fd=None):
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        handles.add(descriptor)
        peak.append(len(handles))
        return descriptor

    def close_directory(descriptor):
        handles.remove(descriptor)
        original_close(descriptor)

    _scan_hook(monkeypatch, "open", open_directory)
    monkeypatch.setattr(processes.os, "close", close_directory)
    with pytest.raises(processes.WorkflowProcessLimitError, match="storage budget"):
        processes.check_storage_budget(root, 1)
    assert not handles and max(peak) == 4


def test_storage_entry_budget_counts_empty_files_directories_and_symlinks(tmp_path):
    (tmp_path / "empty").touch()
    (tmp_path / "directory").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "empty")
    with pytest.raises(processes.WorkflowProcessLimitError, match="storage entry budget"):
        processes.check_storage_budget(tmp_path, 1024, max_files=2)


def test_empty_file_growth_is_detected_and_process_stopped(tmp_path):
    with pytest.raises(processes.WorkflowProcessLimitError, match="storage entry budget"):
        processes.run_process(
            _python("import time; [open(str(n),'wb').close() for n in range(16)]; time.sleep(5)"),
            cwd=tmp_path, storage_root=tmp_path,
            limits=_limits(max_storage_files=8), timeout=5,
        )


def test_process_receives_resource_limits_before_command(tmp_path):
    result = processes.run_process(
        _python(
            "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)); "
            "print(resource.getrlimit(resource.RLIMIT_FSIZE))"
        ),
        cwd=tmp_path, limits=_limits(max_process_cpu_seconds=7, max_process_file_bytes=1024), timeout=5,
    )
    assert result.stdout == b"(7, 7)\n(1024, 1024)\n"


def test_per_file_resource_limit_rejects_growth(tmp_path):
    result = processes.run_process(
        _python("open('file','wb').write(b'x'*8192)"), cwd=tmp_path,
        limits=_limits(max_process_file_bytes=1024), timeout=5,
    )
    assert result.returncode != 0
    assert (tmp_path / "file").stat().st_size <= 1024


def test_linux_root_cannot_claim_process_count_enforcement(monkeypatch, tmp_path):
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes.os, "geteuid", lambda: 0)
    with pytest.raises(processes.WorkflowProcessLimitError, match="unprivileged service UID"):
        processes.run_process(_python("pass"), cwd=tmp_path, timeout=5)


def test_non_posix_platform_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(processes.os, "name", "nt")
    with pytest.raises(processes.WorkflowProcessLimitError, match="POSIX resource limits"):
        processes.run_process(_python("pass"), cwd=tmp_path, timeout=5)


def test_non_linux_resource_boundary_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    with pytest.raises(processes.WorkflowProcessLimitError, match="supported Linux"):
        processes.run_process(_python("pass"), cwd=tmp_path, timeout=5)


def _cgroup_fixture(monkeypatch, tmp_path, *, memory="1024", cpu="200000 100000"):
    mount = tmp_path / "cgroup"
    group = mount / "service"
    group.mkdir(parents=True)
    (group / "memory.max").write_text(memory)
    (group / "cpu.max").write_text(cpu)
    monkeypatch.setattr(processes, "_current_cgroup", lambda: (mount, group))
    return mount, group


def test_aggregate_budget_accepts_finite_kernel_ceilings(monkeypatch, tmp_path):
    _cgroup_fixture(monkeypatch, tmp_path)
    processes._check_aggregate_budgets(1024, 2)


@pytest.mark.parametrize("memory,cpu", [
    ("max", "200000 100000"), ("1025", "200000 100000"),
    ("1024", "max 100000"), ("1024", "200001 100000"),
    ("invalid", "200000 100000"), ("1024", "200000 0"),
])
def test_aggregate_budget_fails_closed_without_sufficient_quota(monkeypatch, tmp_path, memory, cpu):
    _cgroup_fixture(monkeypatch, tmp_path, memory=memory, cpu=cpu)
    with pytest.raises(processes.WorkflowProcessLimitError, match="finite cgroup-v2"):
        processes._check_aggregate_budgets(1024, 2)


def test_aggregate_budget_accepts_inherited_parent_limits(monkeypatch, tmp_path):
    mount, _ = _cgroup_fixture(monkeypatch, tmp_path, memory="max", cpu="max 100000")
    (mount / "memory.max").write_text("1024")
    (mount / "cpu.max").write_text("100000 100000")
    processes._check_aggregate_budgets(1024, 2)


def test_aggregate_budget_requires_visible_v2_hierarchy(monkeypatch):
    def unavailable():
        raise ValueError("No unified membership")
    monkeypatch.setattr(processes, "_current_cgroup", unavailable)
    with pytest.raises(processes.WorkflowProcessLimitError, match="finite cgroup-v2"):
        processes._check_aggregate_budgets(1024, 2)


def test_current_cgroup_resolves_mount_subtree_and_escaped_paths(tmp_path):
    mount = tmp_path / "cgroup mount"
    membership = tmp_path / "membership"
    mounts = tmp_path / "mountinfo"
    membership.write_text("0::/operator/service/child\n")
    mounts.write_text(
        "1036 1035 0:41 /operator " + str(mount).replace(" ", "\\040") +
        " ro,nosuid,nodev,noexec - cgroup2 cgroup rw\n"
    )
    assert processes._current_cgroup(membership=membership, mounts=mounts) == (
        mount, mount / "service" / "child"
    )


def test_current_cgroup_rejects_invisible_membership(tmp_path):
    membership = tmp_path / "membership"
    mounts = tmp_path / "mountinfo"
    membership.write_text("0::/other/service\n")
    mounts.write_text("1036 1035 0:41 /operator /sys/fs/cgroup ro - cgroup2 cgroup rw\n")
    with pytest.raises(ValueError, match="not visible"):
        processes._current_cgroup(membership=membership, mounts=mounts)


@pytest.mark.skipif(sys.platform != "linux", reason="Real quota inheritance requires Linux")
def test_launcher_and_child_inherit_finite_aggregate_kernel_budget(tmp_path):
    result = processes.run_process(
        _python(
            "from pathlib import Path; print(Path('/sys/fs/cgroup/memory.max').read_text().strip()); "
            "print(Path('/sys/fs/cgroup/cpu.max').read_text().strip())"
        ), cwd=tmp_path, timeout=5,
    )
    memory, cpu = result.stdout.decode().splitlines()
    assert int(memory) <= WorkflowInstallationLimits().max_process_tree_memory_bytes
    quota, period = map(int, cpu.split())
    assert quota <= WorkflowInstallationLimits().max_process_tree_cpu_cores * period


@pytest.mark.asyncio
async def test_async_facade_returns_bounded_normal_result(tmp_path):
    result = await processes.run_process_async(_python("print('ok')"), cwd=tmp_path, timeout=5)
    assert result.stdout == b"ok\n"
