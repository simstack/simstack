"""Windows ownership for native science, with atomic Job membership at creation.

Controllers remain outside the science Job. Nested SDK controllers break away
from their parent's Job and receive their own Job handle before bootstrap.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ctypes as C
import hashlib
import json
import os
import subprocess
import weakref

HANDLE, DWORD, SIZE_T = C.c_void_p, C.c_uint32, C.c_size_t
_JOB_HANDLE = "SIMSTACK_WINDOWS_SCIENCE_JOB_HANDLE"
_JOB_NAME = "SIMSTACK_WINDOWS_SCIENCE_JOB"
_BREAKAWAY = 0x01000000
_retained_job = None


class _Limits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", DWORD),
        ("min_working", SIZE_T), ("max_working", SIZE_T), ("active_limit", DWORD),
        ("affinity", SIZE_T), ("priority", DWORD), ("scheduling", DWORD)]


class _ExtendedLimits(C.Structure):
    _fields_ = [("basic", _Limits), ("io", C.c_uint64 * 6),
        ("process_memory", SIZE_T), ("job_memory", SIZE_T),
        ("peak_process_memory", SIZE_T), ("peak_job_memory", SIZE_T)]


class _Accounting(C.Structure):
    _fields_ = [("times", C.c_int64 * 4), ("faults", DWORD), ("total", DWORD),
        ("active", DWORD), ("terminated", DWORD)]


class _Startup(C.Structure):
    _fields_ = [("cb", DWORD), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p),
        ("title", C.c_wchar_p), ("x", DWORD), ("y", DWORD), ("cx", DWORD),
        ("cy", DWORD), ("chars_x", DWORD), ("chars_y", DWORD), ("fill", DWORD),
        ("flags", DWORD), ("show", C.c_uint16), ("reserved_size", C.c_uint16),
        ("reserved_bytes", C.c_void_p), ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE)]


class _StartupEx(C.Structure):
    _fields_ = [("startup", _Startup), ("attributes", C.c_void_p)]


class _ProcessInfo(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


def _kernel():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, C.c_wchar_p], HANDLE),
        "OpenJobObjectW": ([DWORD, C.c_int, C.c_wchar_p], HANDLE),
        "SetInformationJobObject": ([HANDLE, C.c_int, C.c_void_p, DWORD], C.c_int),
        "QueryInformationJobObject": ([HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p], C.c_int),
        "TerminateJobObject": ([HANDLE, C.c_uint], C.c_int),
        "CloseHandle": ([HANDLE], C.c_int),
        "GetCurrentProcess": ([], HANDLE),
        "DuplicateHandle": ([HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), DWORD, C.c_int, DWORD], C.c_int),
        "SetHandleInformation": ([HANDLE, DWORD, DWORD], C.c_int),
        "IsProcessInJob": ([HANDLE, HANDLE, C.POINTER(C.c_int)], C.c_int),
        "InitializeProcThreadAttributeList": ([C.c_void_p, DWORD, DWORD, C.POINTER(SIZE_T)], C.c_int),
        "UpdateProcThreadAttribute": ([C.c_void_p, DWORD, SIZE_T, C.c_void_p, SIZE_T, C.c_void_p, C.c_void_p], C.c_int),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_int, DWORD,
                            C.c_void_p, C.c_wchar_p, C.POINTER(_StartupEx), C.POINTER(_ProcessInfo)], C.c_int),
        "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
        "GetExitCodeProcess": ([HANDLE, C.POINTER(DWORD)], C.c_int),
        "TerminateProcess": ([HANDLE, C.c_uint], C.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _check(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


def job_name(server, entry):
    execution = server.tasks.execution(entry)
    if not all(execution.get(key) for key in ("task_id", "claim_id", "start_id")):
        raise ValueError("A science Job requires its original execution identity")
    bootstrap = server.requests.bootstrap_data or {}
    if not bootstrap.get("account_id"):
        raise ValueError("A science Job requires its bootstrapped account")
    scope = [server.requests.server_url.rstrip("/"), str(bootstrap["account_id"]), execution]
    return "Local\\SimStackScience-" + hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


class WindowsJob:
    def __init__(self, name, *, handle=None, create=False, kernel=None):
        self.kernel = kernel or _kernel()
        self.name, self.handle = name, None
        if handle is not None:
            self.handle = int(handle)
            try:
                _check(self.kernel.SetHandleInformation(self.handle, 1, 0))
                self.active_processes()  # Reject an inherited non-Job handle.
            except BaseException:
                self.close()
                raise
        elif create:
            value = _check(self.kernel.CreateJobObjectW(None, name))
            if C.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: never adopt another launch.
                self.kernel.CloseHandle(value)
                raise FileExistsError("The science Job already exists")
            self.handle = value
            try:
                limits = _ExtendedLimits()
                limits.basic.flags = 0x2000 | 0x0800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
                _check(self.kernel.SetInformationJobObject(value, 9, C.byref(limits), C.sizeof(limits)))
            except BaseException:
                self.close()
                raise
        else:
            value = self.kernel.OpenJobObjectW(0x0004 | 0x0008, False, name)  # QUERY | TERMINATE
            if not value:
                if C.get_last_error() == 2:
                    raise FileNotFoundError(name)
                _check(value)
            self.handle = value

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def active_processes(self):
        info = _Accounting()
        _check(self.kernel.QueryInformationJobObject(self.handle, 1, C.byref(info), C.sizeof(info), None))
        return int(info.active)

    async def stop(self):
        if self.active_processes():
            _check(self.kernel.TerminateJobObject(self.handle, 1))
        while self.active_processes():
            await asyncio.sleep(.05)

    def duplicate_inheritable(self):
        duplicate = HANDLE()
        current = self.kernel.GetCurrentProcess()
        _check(self.kernel.DuplicateHandle(current, self.handle, current, C.byref(duplicate), 0, True, 2))
        return duplicate.value


@contextmanager
def controller_job(server, entry, environment, ownership):
    """Transfer a Job handle before controller bootstrap, retaining caller proof."""
    job = WindowsJob(job_name(server, entry), create=True)
    try:
        duplicate = job.duplicate_inheritable()
    except BaseException:
        job.close()
        raise
    try:
        startup = subprocess.STARTUPINFO()
        previous = ownership.get("startupinfo")
        handles = list((getattr(previous, "lpAttributeList", None) or {}).get("handle_list", []))
        startup.lpAttributeList = {"handle_list": [*handles, duplicate]}
        environment[_JOB_NAME], environment[_JOB_HANDLE] = job.name, str(duplicate)
        yield job, {**ownership, "startupinfo": startup}
    except BaseException:
        job.close()
        raise
    finally:
        job.kernel.CloseHandle(duplicate)


def retain_inherited_job():
    """Keep the controller/managed-worker handle; never pass it to science children."""
    global _retained_job
    handle = os.environ.pop(_JOB_HANDLE, None)
    if handle is not None:
        if not os.environ.get(_JOB_NAME):
            raise ValueError("Inherited science Job has no identity")
        _retained_job = WindowsJob(os.environ[_JOB_NAME], handle=int(handle))
    return _retained_job


def inherited_job(server, entry):
    job = _retained_job or retain_inherited_job()
    if job is None or job.name != job_name(server, entry):
        raise ValueError("Native controller has no matching inherited science Job")
    return job


def child_creationflags():
    """Only the owned science Job may grant a nested SDK controller breakaway."""
    name = os.environ.get(_JOB_NAME)
    if not name:
        return 0
    job = WindowsJob(name)
    try:
        member = C.c_int()
        _check(job.kernel.IsProcessInJob(job.kernel.GetCurrentProcess(), job.handle, C.byref(member)))
        return _BREAKAWAY if member.value else 0
    finally:
        job.close()


class WindowsProcess:
    def __init__(self, info, job):
        self.pid, self._returncode = int(info.pid), None
        self._handle, self._simstack_windows_job = info.process, job
        self.kernel = job.kernel
        self.kernel.CloseHandle(info.thread)
        weakref.finalize(self, self.kernel.CloseHandle, info.process)

    @property
    def returncode(self):
        if self._returncode is None:
            result = self.kernel.WaitForSingleObject(self._handle, 0)
            if result == 0x102:  # WAIT_TIMEOUT: still alive.
                return None
            if result != 0:
                raise OSError("Could not query the scientific Windows process")
            code = DWORD()
            _check(self.kernel.GetExitCodeProcess(self._handle, C.byref(code)))
            self._returncode = int(code.value)
        return self._returncode

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(.05)
        return self.returncode

    async def communicate(self):
        await self.wait()
        return None, None  # Scientific output is redirected to caller-owned files.

    def kill(self):
        _check(self.kernel.TerminateProcess(self._handle, 1))

    terminate = kill


def spawn_job_process(command, *, name, cwd, env, stdout, stderr, startupinfo=None,
                      retain_in_child=False, job=None, creationflags=0):
    """Create science atomically inside its Job (Windows 10+ JOB_LIST attribute)."""
    import msvcrt
    from contextlib import ExitStack
    own_job = job is None
    job = job or WindowsJob(name, create=True)
    if job.name != name:
        raise ValueError("Science process Job differs from its execution")
    kernel, copies, initialized = job.kernel, [], False
    environment = dict(env, **{_JOB_NAME: name})
    environment.pop(_JOB_HANDLE, None)
    try:
        with ExitStack() as files:
            current = kernel.GetCurrentProcess()
            def duplicate_file(value, mode):
                if value == subprocess.DEVNULL:
                    value = files.enter_context(open(os.devnull, mode))
                handle = HANDLE()
                _check(kernel.DuplicateHandle(current, msvcrt.get_osfhandle(value.fileno()), current,
                    C.byref(handle), 0, True, 2))
                copies.append(handle.value)
                return handle.value
            standard = [duplicate_file(subprocess.DEVNULL, "rb"), duplicate_file(stdout, "wb"), duplicate_file(stderr, "wb")]
            inherited = list((getattr(startupinfo, "lpAttributeList", None) or {}).get("handle_list", []))
            if retain_in_child:
                duplicate = job.duplicate_inheritable()
                copies.append(duplicate)
                inherited.append(duplicate)
                environment[_JOB_HANDLE] = str(duplicate)
            handles = (HANDLE * (len(standard) + len(inherited)))(*standard, *inherited)
            jobs = (HANDLE * 1)(job.handle)
            size = SIZE_T()
            kernel.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            if not size.value:
                _check(False)
            attributes = C.create_string_buffer(size.value)
            _check(kernel.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)))
            initialized = True
            _check(kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, C.sizeof(handles), None, None))
            _check(kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, C.sizeof(jobs), None, None))
            startup = _StartupEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = standard
            startup.attributes = C.cast(attributes, C.c_void_p)
            info = _ProcessInfo()
            command_line = C.create_unicode_buffer(subprocess.list2cmdline([str(part) for part in command]))
            block = C.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0")
            _check(kernel.CreateProcessW(None, command_line, None, None, True,
                0x00080000 | 0x00000400 | creationflags, block, str(cwd), C.byref(startup), C.byref(info)))
            process = WindowsProcess(info, job)
            if own_job:
                weakref.finalize(process, job.close)
            return process
    except BaseException:
        if own_job:
            job.close()
        raise
    finally:
        if initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        for handle in copies:
            kernel.CloseHandle(handle)


def job_active_processes(name):
    """Return active membership, or None when no retained Job can be inspected."""
    try:
        job = WindowsJob(name)
    except FileNotFoundError:
        return None
    try:
        return job.active_processes()
    finally:
        job.close()


async def stop_job(name):
    job = WindowsJob(name)
    try:
        await job.stop()
    finally:
        job.close()
