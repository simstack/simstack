"""Windows Job contracts; real kernel checks run on Windows CI only."""
import asyncio
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from simstack.runner_runtime import windows_science_job as jobs


class Kernel:
    def __init__(self):
        self.closed, self.attributes, self.creations, self.calls = [], {}, [], []
        self.next_handle, self.active, self.exited = 200, 0, False

    def CreateJobObjectW(self, security, name):
        self.name = name
        return 100

    def SetInformationJobObject(self, handle, kind, value, size):
        self.limits = C.cast(value, C.POINTER(jobs._ExtendedLimits)).contents.basic.flags
        return 1

    def QueryInformationJobObject(self, handle, kind, value, size, returned):
        C.cast(value, C.POINTER(jobs._Accounting)).contents.active = self.active
        return 1

    def GetCurrentProcess(self):
        return -1

    def DuplicateHandle(self, source_process, source, target_process, target, access, inheritable, flags):
        self.next_handle += 1
        C.cast(target, C.POINTER(jobs.HANDLE)).contents.value = self.next_handle
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        assert count == 2
        C.cast(size, C.POINTER(jobs.SIZE_T)).contents.value = 128
        return bool(attributes)

    def UpdateProcThreadAttribute(self, attributes, flags, kind, values, size, previous, returned):
        self.attributes[kind] = list(C.cast(values, C.POINTER(jobs.HANDLE * (size // C.sizeof(jobs.HANDLE)))).contents)
        self.calls.append(("attribute", kind))
        return 1

    def CreateProcessW(self, executable, command, process_security, thread_security, inherit,
                       flags, environment, cwd, startup, result):
        assert self.attributes[0x2000D] == [100]  # Membership is installed in CreateProcess itself.
        startup = C.cast(startup, C.POINTER(jobs._StartupEx)).contents
        assert startup.startup.flags == 0x100
        self.calls.append(("create",))
        self.creations.append({"flags": flags, "environment": "".join(environment), "inherited": inherit})
        info = C.cast(result, C.POINTER(jobs._ProcessInfo)).contents
        info.process, info.thread, info.pid = 800, 801, 1234
        return 1

    def DeleteProcThreadAttributeList(self, attributes):
        self.calls.append(("delete_attributes",))

    def WaitForSingleObject(self, handle, milliseconds):
        assert milliseconds == 0  # No orphaned infinite wait thread after cancellation.
        return 0 if self.exited else 0x102

    def GetExitCodeProcess(self, handle, result):
        C.cast(result, C.POINTER(jobs.DWORD)).contents.value = 17
        return 1

    def TerminateJobObject(self, handle, code):
        self.calls.append(("terminate", handle))
        self.active = 0
        return 1


@pytest.fixture
def kernel(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(jobs, "_kernel", lambda: kernel)
    monkeypatch.setattr(jobs.C, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda value: value))
    return kernel


def test_windows_native_abi_layout_uses_fixed_width_fields():
    if C.sizeof(C.c_void_p) == 8:
        assert [C.sizeof(value) for value in (jobs._Limits, jobs._ExtendedLimits,
            jobs._Accounting, jobs._Startup, jobs._StartupEx, jobs._ProcessInfo)] == [64, 144, 48, 104, 112, 24]


def test_job_identity_does_not_cross_accounts_or_attempts():
    execution = {"task_id": "task", "generation": "generation", "claim_id": "claim", "start_id": "start"}
    server = SimpleNamespace(requests=SimpleNamespace(server_url="https://server.example", bootstrap_data={"account_id": "a"}),
        tasks=SimpleNamespace(execution=lambda entry: execution))
    names = {jobs.job_name(server, None)}
    server.requests.bootstrap_data["account_id"] = "b"
    names.add(jobs.job_name(server, None))
    execution["start_id"] = "another-start"
    names.add(jobs.job_name(server, None))
    assert len(names) == 3
    execution["start_id"] = None
    with pytest.raises(ValueError, match="execution identity"):
        jobs.job_name(server, None)


def test_atomic_job_spawn_preserves_launch_handles_and_console_flags(kernel, tmp_path):
    startup = SimpleNamespace(lpAttributeList={"handle_list": [777, 778]})
    with tempfile.TemporaryFile() as output:
        process = jobs.spawn_job_process([sys.executable, "-c", "pass"], name="test-job", cwd=tmp_path,
            env={"A": "B"}, stdout=output, stderr=subprocess.DEVNULL, startupinfo=startup,
            retain_in_child=True, creationflags=0x200 | 0x08000000)
    handles = kernel.attributes[0x20002]
    assert 777 in handles and 778 in handles
    environment = kernel.creations[0]["environment"]
    retained = int(next(value.split("=", 1)[1] for value in environment.split("\0")
                        if value.startswith(jobs._JOB_HANDLE + "=")))
    assert retained in handles and retained != 100
    assert kernel.creations[0]["flags"] == 0x80000 | 0x400 | 0x200 | 0x08000000
    assert kernel.limits == 0x2000 | 0x0800
    assert kernel.calls.index(("attribute", 0x2000D)) < kernel.calls.index(("create",))
    assert process.pid == 1234 and process.returncode is None
    kernel.exited = True
    assert process.returncode == 17
    assert 100 not in kernel.closed  # The live observer owns the original Job handle.
    process._simstack_windows_job.close()


def test_failed_atomic_creation_releases_handles_without_fallback(kernel, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("JOB_LIST not supported")
    monkeypatch.setattr(kernel, "CreateProcessW", fail)
    with pytest.raises(OSError, match="JOB_LIST"):
        jobs.spawn_job_process(["python"], name="test-job", cwd=tmp_path, env={},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert 100 in kernel.closed
    assert all(value in kernel.closed for value in kernel.attributes[0x20002])
    assert kernel.calls[-1] == ("delete_attributes",)


@pytest.mark.asyncio
async def test_job_cleanup_is_proof_before_ack_and_errors_propagate(kernel):
    from simstack.core.services.task_termination import stop_owned_process_group
    job = jobs.WindowsJob("job", create=True)
    process = SimpleNamespace(_simstack_windows_job=job)
    kernel.active = 2
    await stop_owned_process_group(process)
    assert kernel.active == 0 and ("terminate", 100) in kernel.calls
    def unavailable(*args):
        raise OSError("job membership cannot be queried")
    kernel.QueryInformationJobObject = unavailable
    with pytest.raises(OSError, match="membership"):
        await stop_owned_process_group(process)
    job.close()


@pytest.mark.asyncio
async def test_cancelling_wait_never_leaves_native_handle_in_wait_thread(kernel, tmp_path):
    process = jobs.spawn_job_process(["python"], name="job", cwd=tmp_path, env={},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    waiter = asyncio.create_task(process.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert process.returncode is None
    process._simstack_windows_job.close()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Job Object API")
@pytest.mark.asyncio
async def test_windows_kernel_orphaned_grandchild_is_stopped_before_ack(tmp_path):
    name = "Local\\SimStackTest-" + uuid4().hex
    marker = tmp_path / "grandchild"
    command = [sys.executable, "-c", "import os,subprocess,sys;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        f"Path({str(marker)!r}).write_text(str(p.pid));os._exit(17)"]
    process = jobs.spawn_job_process(command, name=name, cwd=tmp_path, env=dict(os.environ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert await asyncio.wait_for(process.wait(), 10) == 17
        assert marker.read_text().isdigit()
        assert process._simstack_windows_job.active_processes() > 0
        from simstack.core.services.task_termination import stop_owned_process_group
        await asyncio.wait_for(stop_owned_process_group(process), 10)
        assert process._simstack_windows_job.active_processes() == 0
    finally:
        await process._simstack_windows_job.stop()
        process._simstack_windows_job.close()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Job Object API")
@pytest.mark.asyncio
async def test_windows_kernel_shared_controller_breaks_away_from_parent_job(tmp_path):
    marker = tmp_path / "shared-controller"
    heartbeat = tmp_path / "shared-heartbeat"
    name = "Local\\SimStackTest-" + uuid4().hex
    child = ("import time;from pathlib import Path\nwhile True:\n"
        f" Path({str(heartbeat)!r}).write_text(str(time.monotonic_ns()));time.sleep(.05)")
    code = ("import subprocess,sys,time;from pathlib import Path;"
        "from simstack.runner_runtime.windows_science_job import child_creationflags;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],creationflags=child_creationflags());"
        f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(120)")
    environment = {**os.environ, "PYTHONPATH": str(Path(jobs.__file__).resolve().parents[2])}
    process = jobs.spawn_job_process([sys.executable, "-c", code], name=name, cwd=tmp_path, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shared_pid = None
    try:
        async def ready():
            while not marker.exists() or not marker.read_text().strip():
                await asyncio.sleep(.02)
        await asyncio.wait_for(ready(), 10)
        shared_pid = int(marker.read_text())
        await asyncio.wait_for(process._simstack_windows_job.stop(), 10)
        await process.wait()
        assert jobs.job_active_processes(name) == 0
        async def still_running():
            before = heartbeat.read_text() if heartbeat.exists() else ""
            while not heartbeat.exists() or not heartbeat.read_text().strip() or heartbeat.read_text() == before:
                await asyncio.sleep(.02)
        await asyncio.wait_for(still_running(), 10)
    finally:
        await process._simstack_windows_job.stop()
        process._simstack_windows_job.close()
        if shared_pid is not None:
            subprocess.run(["taskkill", "/PID", str(shared_pid), "/T", "/F"], capture_output=True)


def test_controller_transfers_job_handle_before_bootstrap_without_losing_launch_handles(kernel, monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "STARTUPINFO", SimpleNamespace, raising=False)
    monkeypatch.setattr(jobs, "job_name", lambda *args: "exact-attempt")
    environment = {}
    with jobs.controller_job(None, None, environment,
            {"startupinfo": SimpleNamespace(lpAttributeList={"handle_list": [777]})}) as (job, ownership):
        duplicate = int(environment[jobs._JOB_HANDLE])
        assert ownership["startupinfo"].lpAttributeList["handle_list"] == [777, duplicate]
        assert environment[jobs._JOB_NAME] == "exact-attempt"
        assert duplicate not in kernel.closed and job.handle not in kernel.closed
    assert duplicate in kernel.closed and job.handle not in kernel.closed
    job.close()


@pytest.mark.asyncio
async def test_absent_job_is_unknown_not_a_process_exit_barrier(monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError("no retained handle")
    monkeypatch.setattr(jobs, "WindowsJob", absent)
    assert jobs.job_active_processes("attempt") is None
    with pytest.raises(FileNotFoundError):
        await jobs.stop_job("attempt")


@pytest.mark.asyncio
async def test_owned_windows_job_avoids_pid_tree_and_waits_for_exact_controller_handle(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from simstack.core.services import task_termination as termination
    events = []
    process = SimpleNamespace(pid=42, returncode=None)
    async def stop():
        events.append("science-stopped")
    def kill():
        events.append("controller-killed")
        process.returncode = 1
    process._simstack_windows_job = SimpleNamespace(stop=stop)
    process.kill, process.wait = kill, AsyncMock()
    async def protect(*args):
        events.append("children-detached")
        return {43}
    monkeypatch.setattr(termination, "protected_child_processes", protect)
    tree = Mock(side_effect=AssertionError("owned Job must not use a PID tree"))
    monkeypatch.setattr(termination, "_kill_process_tree", tree)
    entry = SimpleNamespace(job_id=None, parameters=SimpleNamespace(in_docker=False, queue="local"))
    await termination.stop_task_execution(entry, server=None, runner_pid=7, workdir=None, local_process=process)
    assert events == ["children-detached", "science-stopped", "controller-killed"]
    process.wait.assert_awaited_once()
    tree.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows controller crash and Job Object API")
@pytest.mark.asyncio
async def test_windows_kernel_controller_death_retains_exact_science_cleanup_handle(tmp_path):
    execution = {"task_id": uuid4().hex, "generation": "generation", "claim_id": "claim", "start_id": "start"}
    server = SimpleNamespace(requests=SimpleNamespace(server_url="https://server.example", bootstrap_data={"account_id": "test"}),
        tasks=SimpleNamespace(execution=lambda entry: execution))
    marker = tmp_path / "grandchild"
    science = ("import subprocess,sys,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(120)")
    controller = ("import os,subprocess,sys,time;"
        "from simstack.runner_runtime.windows_science_job import retain_inherited_job,spawn_job_process;"
        "job=retain_inherited_job();"
        f"p=spawn_job_process([sys.executable,'-c',{science!r}],name=job.name,job=job,cwd=os.getcwd(),env=dict(os.environ),"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(120)")
    environment = {**os.environ, "PYTHONPATH": str(Path(jobs.__file__).resolve().parents[2])}
    process = None
    with jobs.controller_job(server, None, environment, {}) as (job, ownership):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", controller,
            cwd=tmp_path, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ownership)
    try:
        async def ready():
            while not marker.exists() or not marker.read_text().strip():
                await asyncio.sleep(.02)
        await asyncio.wait_for(ready(), 10)
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        assert job.active_processes() > 0  # Controller exit alone is not physical-stop proof.
        process._simstack_windows_job = job
        from simstack.core.services.task_termination import stop_owned_process_group
        await asyncio.wait_for(stop_owned_process_group(process), 10)
        assert job.active_processes() == 0
    finally:
        await job.stop()
        if process.returncode is None:
            process.kill()
        await process.wait()
        job.close()
