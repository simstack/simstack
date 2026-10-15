import asyncio
import hashlib
import logging
import os
import platform
from pathlib import Path

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
from simstack.runner_runtime.environment import sanitized_trusted_helper_environment

logger = logging.getLogger("NodeRunner")


def runner_pid_file(workdir: Path, resource: object) -> Path:
    """Map an opaque server resource name to one contained deterministic path."""

    resolved_workdir = Path(workdir).resolve()
    resource_digest = hashlib.sha256(str(resource).encode("utf-8")).hexdigest()
    pid_file = (resolved_workdir / f"runner-{resource_digest}.pid").resolve()
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
        self._is_default = is_default
        self._with_file_transfer = with_file_transfer

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running and is a simstack_runner process"""
        try:
            if platform.system() == "Windows":
                # On Windows, os.kill with signal 0 doesn't work, use tasklist
                import subprocess

                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/V", "/FO", "CSV"],
                    capture_output=True,
                    text=False,  # keep bytes; avoid UnicodeDecodeError from console code pages
                    env=sanitized_trusted_helper_environment(),
                )
                # Check if process exists and command line contains simstack_runner
                stdout = result.stdout.decode("utf-8", errors="ignore")
                return str(pid) in stdout and "simstack_runner" in stdout.lower()
            else:
                # Check if process exists
                os.kill(pid, 0)
                # Verify it's a simstack_runner process by checking command line
                try:
                    with open(f"/proc/{pid}/cmdline", "r") as f:
                        cmdline = f.read()
                        return "simstack_runner" in cmdline
                except (FileNotFoundError, PermissionError):
                    # If we can't read cmdline, fall back to just checking if process exists
                    return True
        except (OSError, ProcessLookupError):
            return False

    def _check_existing_runner(self) -> None:
        """Check if another runner for this resource is already running on this host"""
        if self._pid_file.is_symlink():
            raise RuntimeError("Runner PID file cannot be a symbolic link")
        if self._pid_file.exists():
            try:
                existing_pid = int(self._pid_file.read_text().strip())
                if existing_pid != self._pid and self._is_process_running(existing_pid):
                    error_msg = (
                        f"Another runner for resource '{self._resource}' is already running "
                        f"on this host with PID {existing_pid}. Exiting."
                    )
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)
                else:
                    logger.info(
                        f"Stale PID file found for resource '{self._resource}'. "
                        f"Overwriting with current PID {self._pid}."
                    )
            except (ValueError, OSError) as e:
                logger.warning(
                    f"Could not read PID file: {e}. Proceeding with startup."
                )

    def _write_pid_file(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._pid_file, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, str(self._pid).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)

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
        # Check if another runner is already running for this resource
        self._check_existing_runner()

        # Save PID on startup
        self._write_pid_file()

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

        # Add timeout restart service if timeout is specified
        if timeout is not None:
            self._services.append(
                TimeoutRestartService(
                    self._resource,
                    timeout,
                    shutdown_event=self._shutdown_event,
                )
            )

        # Start all services
        service_tasks = [service.start() for service in self._services]
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        tasks_to_wait = [*service_tasks, shutdown_task]

        try:
            # Wait for either a service task to finish or the shutdown event to be set
            await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self.stop_all_services()
            for task in tasks_to_wait:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
