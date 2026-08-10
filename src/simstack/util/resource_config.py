import tomllib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

from odmantic import ObjectId

import logging
logger = logging.getLogger("ResourceConfig")

class ResourceConfig:
    """
    ResourceConfig is responsible for managing configuration settings, setup,
    execution, and post-processing parameters for specified resources.

    This class is designed to read configuration data from a TOML file, provide
    an interface to access resource-specific parameters, and execute resource-related
    operations such as setup and running commands. It encapsulates functionality for
    handling temporary directories, file manipulation, and subprocess execution.

    Attributes:
        _config (Dict[str, Any]): The loaded configuration dictionary.
        _resource (str): The name of the current resource.
    """
    def __init__(self, config_path: Path, resource: str):
        self._config: Dict[str, Any] = {}
        self._resource = resource

        actual_path = Path(config_path)
        if actual_path.is_dir():
            actual_path = actual_path / "config.toml"

        if actual_path.exists():
            with open(actual_path, "rb") as f:
                self._config = tomllib.load(f)
        else:
            # If it doesn't exist, we just have an empty config
            pass

    @property
    def os(self) -> str:
        """
        Returns the OS of the current resource, defaults to 'linux'.
        """
        try:
            return self._config[self._resource].get("os", "linux")
        except KeyError:
            return "linux"

    def setup(self, node_runner: Optional[Any] = None):
        """
        Executes the setup scripts for the current resource.
        """
        setup_params = self.get_setup_params()
        scripts = setup_params.get("scripts", [])
        for i, script in enumerate(scripts):
            # Note: shell=True is needed to execute shell commands/scripts
            # and might be OS dependent for the actual commands.
            if node_runner and hasattr(node_runner, "subprocess"):
                node_runner.subprocess(f"setup_{i}", script)
            else:
                subprocess.run(script, shell=True, check=True)

    def tmp_dir(self, task_id: ObjectId | str) -> Path:
        if isinstance(task_id, ObjectId):
            task_id = str(task_id)
        tmp_dir = self.tmp_base_dir / str(task_id)
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        return tmp_dir

    @property
    def tmp_base_dir(self) -> Path:
        tmp_base_dir_str = self.get_setup_params().get("tmp_base_dir", "")

        if tmp_base_dir_str:
            expanded_path_str = os.path.expandvars(os.path.expanduser(tmp_base_dir_str))
            path = Path(expanded_path_str)
            path.mkdir(parents=True, exist_ok=True)
            return path

        # Default to system temp directory if no command/path specified
        return Path(tempfile.gettempdir())

    def run(self,
        program_name: str,
        input_files: Optional[List[Union[str, "FileStack"]]] = None,
        output_files: Optional[List[Union[str, "FileStack"]]] = None,
        node_runner: Optional[Any] = None,
    ):
        """
        Executes the run command with optional temporary directory usage and file handling.
        Retrieves parameters from the configuration for the specified program.

        Args:
            program_name: Name of the program to run.
            input_files: List of input files (str or FileStack). Overrides TOML input_files if provided.
            output_files: List of output files (str or FileStack). Overrides TOML output_files if provided.
            node_runner: Optional NodeRunner instance for execution.
        """

        params = self.get_program(program_name)
        run_command = params.get("run_command", "")
        
        if input_files is None:
            input_files = params.get("input_files", [])
        
        if output_files is None:
            output_files = params.get("output_files", [])
            
        use_temp = params.get("use_temp", False)
        
        # tmp_base_dir can come from setup or the program itself, but usually it's in setup for the resource
    
        # scratch_cleanup from postprocessing
        post_params = self.get_postprocessing_params()
        scratch_cleanup = params.get("scratch_cleanup", post_params.get("scratch_cleanup", False))

        tmp_dir = None
        try:
            exec_dir = Path.cwd()
            if use_temp:
                tmp_id = node_runner.task_id if node_runner else uuid.uuid4()
                tmp_dir = self.tmp_dir(tmp_id)
                exec_dir = tmp_dir
                
                # Copy input files to exec_dir
                for f in input_files:
                    if hasattr(f, "get"):  # It's a FileStack
                        f.get(local_dir=exec_dir)
                    else:  # It's a string (filename)
                        src = Path.cwd() / f
                        if src.exists() and src != exec_dir / f:
                            shutil.copy(src, exec_dir / f)

            # Execute run_command
            if node_runner and hasattr(node_runner, "subprocess"):
                node_runner.subprocess("run", run_command, cwd=str(exec_dir))
            else:
                subprocess.run(run_command, shell=True, check=True, cwd=exec_dir)


            # Copy output files back to cwd
            if use_temp and tmp_dir:
                for f in output_files:
                    # output_files can also be FileStack in the new pattern?
                    # "output_files: List[str | FileStack] = [...]"
                    # If it's a FileStack, we might need to update its content from the local file
                    # but the standard behavior for output_files here is copying back to cwd.
                    filename = f.name if hasattr(f, "name") else f
                    src = tmp_dir / filename
                    if src.exists():
                        shutil.copy(src, Path.cwd() / filename)

        finally:
            if scratch_cleanup and tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    def get_program(self, program_name: str, resource: str | None = None) -> Dict[str, Any]:
        """
        Returns the dict from resource.program.name for program with name.
        Expected structure in TOML: [resource_name.program.program_name]

        If ``resource`` is omitted, uses the ResourceConfig's current resource.
        """
        lookup = resource if resource is not None else self._resource
        try:
            return self._config[lookup]["program"][program_name]
        except (KeyError, TypeError):
            return {}

    def get_setup_params(self) -> Dict[str, Any]:
        """
        Returns the setup dict for the specified resource.
        Expected structure in TOML: [resource_name.setup]
        """
        try:
            return self._config[self._resource]["setup"]
        except KeyError:
            return {}

    def get_postprocessing_params(self) -> Dict[str, Any]:
        """
        Returns the post-processing dict for the specified resource.
        Expected structure in TOML: [resource_name.post-processing] or [resource_name.postprocessing]
        """
        try:
            resource_cfg = self._config[self._resource]
            if "post-processing" in resource_cfg:
                return resource_cfg["post-processing"]
            if "postprocessing" in resource_cfg:
                return resource_cfg["postprocessing"]
        except KeyError:
            pass
        return {}

    def __str__(self):
        return f"ResourceConfig(resource={self._resource}, config={self._config})"