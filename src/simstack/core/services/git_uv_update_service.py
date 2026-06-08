import asyncio
import logging
from pathlib import Path
import hashlib

from simstack.core.context import context
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEventEnum
from simstack.core.services.base_service import RestartService

logger = logging.getLogger("NodeRunner")


def get_file_checksum(filepath: Path) -> str:
    """Calculate SHA256 checksum of a file"""
    if not filepath.exists():
        return ""
    hash_sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


class GitUvUpdateService(RestartService):
    """
    Advanced service that performs git pull and uv lock upgrades.
    If changes are detected, it restarts the runner.
    """

    def __init__(self, resource: Resource, interval: int) -> None:
        super().__init__("GitUvUpdate", resource, interval)
        # Resolve project root (assuming we are in src/simstack/core/runner.py)
        self._project_dir = context.config.project_root.resolve(strict=True)
        self._uv_lock_path = context.config.project_root / "uv.lock"

    async def _run_command(self, cmd: list[str], ignore_error: bool = False) -> str:
        """Run a shell command and return output"""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            if ignore_error:
                logger.warning(f"Command {' '.join(cmd)} failed: {stderr.decode()}")
            else:
                raise RuntimeError(f"Command {' '.join(cmd)} failed: {stderr.decode()}")
        return stdout.decode().strip()

    async def execute(self) -> None:
        # 1. Git Pull
        # Check checksum before and after pull to see if Git brought a new lockfile
        # old_uv_checksum = get_file_checksum(self._uv_lock_path)

        # Ensure we don't have local lockfile changes that block the pull

        await self._run_command(["git", "stash"], ignore_error=True)

        git_output = await self._run_command(["git", "pull"])
        git_changed = "Already up to date." not in git_output

        # Clear the stash now that we've pulled
        # await self._run_command(["git", "stash", "drop"], ignore_error=True)

        if git_changed:  # or uv_locally_upgraded:
            await self._run_command(["uv", "sync", "--locked"])  # Update local .venv
            reason = "Git pull" if git_changed else "Local UV upgrade"
            await self.write_resource_event(RunnerEventEnum.SHUTDOWN, message=reason)
            logger.info(f"Update detected ({reason}). Triggering restart...")
            await self.trigger_restart()
