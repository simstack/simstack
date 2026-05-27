import asyncio
import logging
import os
import platform
import sys

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.core.services.node_execution_service import NodeExecutionService
from simstack.core.services.runner_status_service import RunnerStatusService
from simstack.core.services.runner_cleanup_service import RunnerCleanupService
from simstack.core.services.file_transfer_service import FileTransferService
from simstack.core.services.slurm_status_service import SlurmStatusService
from simstack.core.services.resource_branch_monitor_service import ResourceBranchMonitorService
from simstack.core.services.stop_check_service import StopCheckService
from simstack.core.services.git_uv_update_service import GitUvUpdateService
from simstack.core.services.timeout_restart_service import TimeoutRestartService

logger = logging.getLogger("NodeRunner")

class RunnerManager:
    def __init__(self, resource: Resource, detach: bool = True, no_pull: bool = False,
                 is_default: bool = False):
        self._resource = resource
        self._detach = detach
        self._no_pull = no_pull
        self._pid = os.getpid()
        self._services = []
        self._shutdown_event = asyncio.Event()
        self._pid_file = context.config.workdir / f"runner_{resource}.pid"
        self._is_default = is_default

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

    def _check_existing_runner(self):
        """Check if another runner for this resource is already running on this host"""
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
                logger.warning(f"Could not read PID file: {e}. Proceeding with startup.")

    async def stop_all_services(self):
        """Gracefully stop all registered services"""
        logger.info("Stopping all services...")
        await asyncio.gather(*(s.stop() for s in self._services), return_exceptions=True)

    async def run_nodes_for_resource(self, polling_interval=5, max_concurrent=10, timeout=None):
        """Orchestrates multiple independent services"""
        # Check if another runner is already running for this resource
        self._check_existing_runner()

        # Save PID on startup
        self._pid_file.write_text(str(self._pid))

        self._services = [
            NodeExecutionService(self._resource, polling_interval, max_concurrent, self._shutdown_event,
                                 detach=self._detach, is_default=self._is_default),
            FileTransferService(self._resource, interval=10, max_concurrent=2, shutdown_event=self._shutdown_event),
            RunnerStatusService(self._resource, interval=60),
            RunnerCleanupService(self._resource, interval=300),
            SlurmStatusService(self._resource, interval=60),
            StopCheckService(self._resource, interval=10, shutdown_event=self._shutdown_event),
        ]

        if not self._no_pull:
            self._services.append(GitUvUpdateService(self._resource, interval=60))
            self._services.append(ResourceBranchMonitorService(self._resource, interval=60))

        # Add timeout restart service if timeout is specified
        if timeout is not None:
            self._services.append(TimeoutRestartService(self._resource, timeout))

        # Start all services
        service_tasks = [service.start() for service in self._services]

        try:
            # Wait for either a service task to finish or the shutdown event to be set
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            tasks_to_wait = [*service_tasks, shutdown_task]
            done, pending = await asyncio.wait(
                tasks_to_wait,
                return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            await self.stop_all_services()
