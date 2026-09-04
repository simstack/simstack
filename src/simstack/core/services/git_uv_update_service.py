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

    def __init__(self, resource: Resource, interval):
        super().__init__("GitUvUpdate", resource, interval)
        # Resolve project root (assuming we are in src/simstack/core/runner.py)
        self._project_dir = context.config.project_root.resolve(strict=True)
        self._uv_lock_path = context.config.project_root / "uv.lock"
        self._uv_extra_depencency_path = context.config.project_root / "user_extra_config.toml"
        self.extras=False
        self.desired_extras=[]
        #check if the file _uv_extra_depencency_path exists if yes - read toml and then extras=True
        try :
            import tomllib
            logger.info(f" trying to read user-specified (extras) optional dependencies from {self._uv_extra_depencency_path}")
            with open (self._uv_extra_depencency_path,"rb") as f:
                desired_extras_all=tomllib.load(f)
                # Access the nested section - this was the issue with the keys
                extras_section = desired_extras_all.get("optional_dependencies_desired_by_ressource", {})

            
            # Access raw value without triggering validation against allowed_resources
            resource_name = object.__getattribute__(resource, "__dict__").get("value") or str(resource)
            #logger.info(f" name {resource_name} , type {type(resource_name)}, compared against {extras_section.keys()}") 
            if resource_name in extras_section.keys():
                self.desired_extras = extras_section[resource_name]
                self.extras = True
                
        except Exception as e:
            logger.warning(f"resource {resource} spec  with user file {self._uv_extra_depencency_path} failed with the exception {e}")


        

    async def _run_command(self, cmd: list, ignore_error=False, retries=0, backoff=2) -> str:
        """Run a shell command and return output with optional retries"""
        for attempt in range(retries + 1):
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return stdout.decode().strip()

            err_msg = stderr.decode()
            if attempt < retries:
                wait_time = backoff ** (attempt + 1)
                logger.warning(f"Command {' '.join(cmd)} failed (attempt {attempt+1}/{retries+1}): {err_msg}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                if ignore_error:
                    logger.warning(f"Command {' '.join(cmd)} failed after {retries+1} attempts: {err_msg}")
                    return ""
                else:
                    raise RuntimeError(f"Command {' '.join(cmd)} failed after {retries+1} attempts: {err_msg}")
        return ""
    
    async def execute(self):
        # 1. Git Pull
        # Check checksum before and after pull to see if Git brought a new lockfile
        # old_uv_checksum = get_file_checksum(self._uv_lock_path)
        
        # Ensure we don't have local lockfile changes that block the pull
        await self._run_command(["git", "stash"], ignore_error=True)

        # Fetch all remotes to ensure we have the latest refs, avoiding "no such ref was fetched" errors
        # Using retries for fetch/pull as SSH connections might be closed by remote host (rate limiting)
        await self._run_command(["git", "fetch", "--all", "--prune"], retries=3)

        git_output = await self._run_command(["git", "pull", "--recurse-submodules" ], retries=3)
        git_changed = "Already up to date." not in git_output

        # Clear the stash now that we've pulled
        # await self._run_command(["git", "stash", "drop"], ignore_error=True)

        if git_changed: # or uv_locally_upgraded:
            command_list=["uv", "sync", "--locked"]
            if self.extras and len(self.desired_extras) > 0:
                
                for desired_extra in self.desired_extras:
                    command_list.extend(["--extra", str(desired_extra)])
                    logger.info(f"user specified extra dependency {desired_extra}")
                    
                await self._run_command(command_list)  # Update local .venv but with extras as specified by the user for the ressource
            else:
                await self._run_command(command_list)  # Update local .venv
            reason = "Git pull" if git_changed else "Local UV upgrade"
            await self.write_resource_event(RunnerEventEnum.SHUTDOWN, message=reason)
            logger.info(f"Update detected ({reason}). Triggering restart...")
            await self.trigger_restart()
