import asyncio
import errno
import hashlib
import logging
import os
from pathlib import Path
import stat

from simstack.core.services.base_service import BaseService
from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.core.services.runner_status_service import RunnerStatusService
from simstack.core.services.file_transfer_service import FileTransferService
from simstack.core.services.slurm_status_service import SlurmStatusService
from simstack.core.services.stop_check_service import StopCheckService
from simstack.core.services.timeout_restart_service import TimeoutRestartService
from simstack.core.services.workflow_installation_service import WorkflowInstallationService

logger = logging.getLogger("NodeRunner")


def runner_pid_file(workdir: Path, resource: object) -> Path:
    """Map an opaque server resource name to one contained deterministic path."""

    resolved_workdir = Path(workdir).resolve()
    resource_digest = hashlib.sha256(str(resource).encode("utf-8")).hexdigest()
    # Resolve the operator-owned parent, not the final component: resolving a
    # pre-existing symlink here would bypass the no-follow open below.
    pid_file = resolved_workdir / f"runner-{resource_digest}.pid"
    try:
        pid_file.relative_to(resolved_workdir)
    except ValueError as exc:  # pragma: no cover - the digest is a safe component
        raise RuntimeError("Runner PID path escapes configured workdir") from exc
    return pid_file


class RunnerManager:
    def __init__(
        self,
        resource: Resource,
        is_default: bool = False,
        with_file_transfer: bool = True,
    ) -> None:
        self._resource = resource
        self._pid = os.getpid()
        self._services: list[BaseService] = []
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._pid_file = runner_pid_file(Path(context.config.workdir), resource)
        self._pid_file_fd: int | None = None
        self._is_default = is_default
        self._with_file_transfer = with_file_transfer

    def _acquire_runner_lock(self) -> None:
        """The kernel lock, never a reusable PID/TID, owns Runner exclusivity."""
        if self._pid_file_fd is not None:
            raise RuntimeError(f"Another runner for resource '{self._resource}' is already running")
        descriptor = None
        try:
            try:
                before = self._pid_file.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise RuntimeError("Runner PID file must be a regular file, not a symbolic link")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self._pid_file, flags, 0o600)
            opened = os.fstat(descriptor)
            current = self._pid_file.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise RuntimeError("Runner PID file must be one unchanged regular file")
            os.set_inheritable(descriptor, False)
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}:
                    raise RuntimeError(f"Another runner for resource '{self._resource}' is already running") from exc
                raise
            self._pid_file_fd = descriptor
            descriptor = None
            try:
                self._write_pid_file()
            except BaseException:
                self._release_runner_lock()
                raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write_pid_file(self) -> None:
        """Publish informational PID only after acquiring the lifetime lock."""
        fd = self._pid_file_fd
        if fd is None:
            raise RuntimeError("Runner PID file must be locked before writing")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        content = str(self._pid).encode("ascii")
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if not written:
                raise OSError("Runner PID file write made no progress")
            remaining = remaining[written:]
        os.ftruncate(fd, len(content))
        os.fsync(fd)

    def _release_runner_lock(self) -> None:
        descriptor, self._pid_file_fd = self._pid_file_fd, None
        if descriptor is not None:
            # Close releases POSIX flock and Windows byte locks, including after
            # a crash. Never unlink: a new inode would permit two lock owners.
            os.close(descriptor)

    async def stop_all_services(self) -> None:
        """Gracefully stop all registered services"""
        logger.info("Stopping all services...")
        await asyncio.gather(
            *(s.stop() for s in self._services), return_exceptions=True
        )

    async def run_nodes_for_resource(
        self,
        polling_interval: int = 5,
        max_concurrent: int = 10,
        timeout: int | None = None,
    ) -> None:
        """Orchestrates multiple independent services"""
        self._acquire_runner_lock()
        tasks_to_wait = []
        try:
            self._shutdown_event.clear()
            self._services = [
                NodeExecutionService(
                    self._resource,
                    polling_interval,
                    max_concurrent,
                    self._shutdown_event,
                    is_default=self._is_default,
                ),
                RunnerStatusService(self._resource, interval=60),
                SlurmStatusService(
                    self._resource,
                    interval=60,
                    max_concurrent=max_concurrent,
                ),
                StopCheckService(
                    self._resource, interval=10, shutdown_event=self._shutdown_event
                ),
            ]

            if getattr(context.config, "workflow_installations", False):
                self._services.append(WorkflowInstallationService(self._resource, polling_interval, self._shutdown_event))

            if self._with_file_transfer:
                self._services.append(
                    FileTransferService(
                        self._resource,
                        interval=10,
                        max_concurrent=2,
                        shutdown_event=None,
                    )
                )

            if timeout is not None:
                self._services.append(
                    TimeoutRestartService(
                        self._resource,
                        timeout,
                        shutdown_event=self._shutdown_event,
                    )
                )

            for service in self._services:
                tasks_to_wait.append(service.start())
            tasks_to_wait.append(asyncio.create_task(self._shutdown_event.wait()))
            # Wait for either a service task to finish or the shutdown event to be set
            await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
        finally:
            async def cleanup():
                try:
                    await self.stop_all_services()
                finally:
                    for task in tasks_to_wait:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks_to_wait, return_exceptions=True)

            cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            try:
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup_task.result()
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                self._release_runner_lock()
