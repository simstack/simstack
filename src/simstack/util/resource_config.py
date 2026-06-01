import tomllib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

class ResourceConfig:
    """
    ResourceConfig is responsible for managing configuration settings, setup,
    execution, and post-processing parameters for specified resources.

    This class is designed to read configuration data from a TOML file, provide
    an interface to access resource-specific parameters, and execute resource-related
    operations such as setup and running commands. It encapsulates functionality for
    handling temporary directories, file manipulation, and subprocess execution.

    Attributes:
        os (str): The operating system associated with the resource, with a default value of "linux".
    """
    def __init__(self, config_path: Path, resource: str):
        self._config: Dict[str, Any] = {}
        self._resource = resource
        self._program = None

        actual_path = config_path
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

    @property
    def program(self) -> Optional[str]:
        return self._program

    @program.setter
    def program(self, value: str):
        self._program = value

    def run(
        self,
        program_name: Optional[str] = None,
        input_files: Optional[List[Union[str, "FileStack"]]] = None,
        output_files: Optional[List[Union[str, "FileStack"]]] = None,
        node_runner: Optional[Any] = None,
    ):
        """
        Executes the run command with optional temporary directory usage and file handling.
        Retrieves parameters from the configuration for the specified program.

        Args:
            program_name: Name of the program to run. Overrides self.program if provided.
            input_files: List of input files (str or FileStack). Overrides TOML input_files if provided.
            output_files: List of output files (str or FileStack). Overrides TOML output_files if provided.
            node_runner: Optional NodeRunner instance for execution.
        """
        if program_name:
            self._program = program_name

        params = self.get_program()
        run_command = params.get("run_command", "")
        
        if input_files is None:
            input_files = params.get("input_files", [])
        
        if output_files is None:
            output_files = params.get("output_files", [])
            
        use_temp = params.get("use_temp", False)
        
        # tmp_base_dir can come from setup or the program itself, but usually it's in setup for the resource
        setup_params = self.get_setup_params()
        tmp_base_dir = params.get("tmp_base_dir", setup_params.get("tmp_base_dir"))
        
        # scratch_cleanup from postprocessing
        post_params = self.get_postprocessing_params()
        scratch_cleanup = params.get("scratch_cleanup", post_params.get("scratch_cleanup", False))

        cwd = Path.cwd()
        tmp_dir = None
        
        try:
            if use_temp:
                if tmp_base_dir:
                    # In config.toml, tmp_base_dir might be a command like "set TMP_BASE_DIR=..." or "TMP_BASE_DIR=..."
                    # But the requirement says "based on tmp_base_dir", implying it's a path.
                    # Let's try to extract the path if it looks like a variable assignment.
                    base_path = tmp_base_dir
                    if "=" in tmp_base_dir:
                        base_path = tmp_base_dir.split("=")[-1].strip().strip('"').strip("'")
                    
                    if not os.path.exists(base_path):
                        os.makedirs(base_path, exist_ok=True)
                    tmp_dir = Path(tempfile.mkdtemp(dir=base_path))
                else:
                    tmp_dir = Path(tempfile.mkdtemp())
                
                exec_dir = tmp_dir
            else:
                exec_dir = cwd

            # Copy input files to exec_dir
            for f in input_files:
                if hasattr(f, "get"):  # It's a FileStack
                    f.get(local_dir=exec_dir)
                else:  # It's a string (filename)
                    src = cwd / f
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
                        shutil.copy(src, cwd / filename)

        finally:
            if scratch_cleanup and tmp_dir and tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    def get_program(self) -> Dict[str, Any]:
        """
        Returns the dict from resource.program.name for program with name and the current resource.
        Expected structure in TOML: [resource_name.program.program_name]
        """
        try:
            # We don't have program_name anymore in the method signature, 
            # so we need to know what it is. 
            # If the instruction says "remove the program_name parameter", 
            # maybe it refers to get_program too?
            # But how would it know WHICH program?
            # Maybe it's stored in self._program?
            return self._config[self._resource]["program"][self._program]
        except (KeyError, AttributeError):
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
