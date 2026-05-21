import asyncio
import logging

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.models.resource_definition import ResourceDefinition
from simstack.core.services.base_service import RestartService

logger = logging.getLogger("NodeRunner")

class ResourceBranchMonitorService(RestartService):
    """
    Monitors the ResourceDefinition for the current resource.
    If the git_branch field changes, it stashes, switches branch, syncs, and restarts.

    def __init__(self, resource: Resource, interval: int):
        super().__init__("ResourceBranchMonitor", resource, interval)
        self._project_dir = context.config.project_root.resolve(strict=True)

    async def _run_command(self, cmd: list) -> str:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(f"Command {' '.join(cmd)} failed: {stderr.decode()}")
            return ""
        return stdout.decode().strip()

    async def _get_current_branch(self) -> str:
        return await self._run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])

    async def execute(self):
        resource_def = await context.db.find_one(
            ResourceDefinition, 
            ResourceDefinition.resource_str == str(self._resource)
        )
        
        if not resource_def:
            return

        target_branch = resource_def.git_branch
        current_branch = await self._get_current_branch()

        if target_branch and current_branch and target_branch != current_branch:
            logger.info(f"Branch change detected: {current_branch} -> {target_branch}. Updating...")
            
            # 1. Stash existing changes
            await self._run_command(["git", "stash"])
            
            # 2. Checkout new branch
            checkout_res = await self._run_command(["git", "checkout", target_branch])
            if not checkout_res:
                logger.error(f"Failed to checkout branch {target_branch}")
                return

            # 3. UV Sync
            logger.info("Running uv sync --locked...")
            await self._run_command(["uv", "sync", "--locked"])

            # 4. Trigger Restart
            await self.write_resource_event(
                RunnerEventEnum.SHUTDOWN, 
                message=f"Branch switched to {target_branch}"
            )
            await self.trigger_restart()
