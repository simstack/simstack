import copy
import tomllib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

from odmantic import ObjectId

import logging
logger = logging.getLogger("ResourceConfig")


def _deep_merge_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged

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
        self._config_path = actual_path
        self.reload()

    def reload(self) -> None:
        """Re-read ``config.toml`` from disk.

        The runner keeps a long-lived ``ResourceConfig``. Git pull (or a local
        edit) can add or change ``docker_image`` assignments after startup;
        callers that launch containers must reload before looking them up.
        """
        if self._config_path.exists():
            with open(self._config_path, "rb") as f:
                self._config = tomllib.load(f)
        else:
            self._config = {}

    def _resource_section(self, resource: str) -> Dict[str, Any]:
        """Return the resource table after following ``same-as`` aliases.

        ``same-as`` copies another resource's table. Extra keys on the aliasing
        resource overlay the target (nested tables are merged). A missing
        resource raises ``ValueError``. A ``same-as`` that points at a missing
        resource, is not a non-empty string, or forms a cycle also raises
        ``ValueError``.
        """
        seen: list[str] = []
        overlays: list[Dict[str, Any]] = []
        current = resource
        while True:
            if current in seen:
                chain = " -> ".join(seen + [current])
                raise ValueError(f"Circular same-as in config.toml: {chain}")
            seen.append(current)
            section = self._config.get(current)
            if section is None:
                if len(seen) == 1:
                    raise ValueError(
                        f"config.toml has no [{current}] resource block"
                    )
                raise ValueError(
                    f"config.toml resource {current!r} referenced by same-as "
                    f"from {seen[-2]!r} does not exist"
                )
            if not isinstance(section, dict):
                raise ValueError(
                    f"config.toml resource {current!r} must be a table, "
                    f"got {type(section).__name__}"
                )
            hyphen_alias = section.get("same-as")
            underscore_alias = section.get("same_as")
            if (
                hyphen_alias is not None
                and underscore_alias is not None
                and hyphen_alias != underscore_alias
            ):
                raise ValueError(
                    f"config.toml resource {current!r} has conflicting "
                    f"same-as={hyphen_alias!r} and same_as={underscore_alias!r}"
                )
            alias = hyphen_alias if hyphen_alias is not None else underscore_alias
            if alias is None:
                resolved = copy.deepcopy(section)
                for overlay in reversed(overlays):
                    resolved = _deep_merge_dicts(resolved, overlay)
                resolved.pop("same-as", None)
                resolved.pop("same_as", None)
                return resolved
            if not isinstance(alias, str) or alias == "":
                raise ValueError(
                    f"same-as for resource {current!r} must be a non-empty "
                    f"string, got {alias!r}"
                )
            overlays.append(
                {
                    k: v
                    for k, v in section.items()
                    if k not in ("same-as", "same_as")
                }
            )
            current = alias

    @property
    def os(self) -> str:
        """
        Returns the OS of the current resource, defaults to 'linux'.
        """
        section = self._resource_section(self._resource)
        if "os" not in section:
            return "linux"
        return section["os"]

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
        configured = self.get_setup_params().get("tmp_base_dir", None)
        candidates = []
        if configured:
            text = str(configured).strip()
            for match in re.finditer(
                r'(?:^|\n)\s*(?:set\s+|export\s+)?TMP_BASE_DIR\s*=\s*["\']?([^\n"\']+)',
                text,
                re.IGNORECASE,
            ):
                candidates.append(match.group(1).strip())
            if "\n" not in text and not re.search(r"\bif\b", text, re.IGNORECASE):
                if not re.search(r"TMP_BASE_DIR\s*=", text, re.IGNORECASE):
                    candidates.append(text)
        env_tmp_base = os.environ.get("TMP_BASE_DIR")
        if env_tmp_base:
            candidates.append(env_tmp_base)

        last_error = None
        for raw in candidates:
            expanded = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
            if not expanded or "$" in expanded or (os.name == "nt" and "%" in expanded):
                continue
            path = Path(expanded)
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError as exc:
                last_error = exc

        if configured:
            raise ValueError(
                f"Could not resolve tmp_base_dir from {configured!r}"
                + (f": {last_error}" if last_error else "")
            )
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

        if "use_tmp" in params and "use_temp" in params:
            if bool(params["use_tmp"]) != bool(params["use_temp"]):
                raise ValueError(
                    f"Program {program_name!r} has conflicting use_tmp="
                    f"{params['use_tmp']!r} and use_temp={params['use_temp']!r}"
                )
        if "use_tmp" in params:
            use_temp = params["use_tmp"]
        elif "use_temp" in params:
            use_temp = params["use_temp"]
        else:
            use_temp = False
        if not isinstance(use_temp, bool):
            raise ValueError(
                f"use_tmp/use_temp for program {program_name!r} must be a bool, "
                f"got {use_temp!r}"
            )

        if node_runner is not None:
            from simstack.core.node_runner import NodeRunner

            if isinstance(node_runner, NodeRunner):
                if use_temp:
                    node_runner.stage(input_files=input_files)
                    node_runner.execute(program_name)
                    node_runner.retrieve(output_files=output_files)
                else:
                    node_runner.execute(program_name)
                return

        # scratch_cleanup from postprocessing
        post_params = self.get_postprocessing_params()
        scratch_cleanup = params.get("scratch_cleanup", post_params.get("scratch_cleanup", False))

        tmp_dir = None
        try:
            exec_dir = Path.cwd()
            if use_temp:
                if not self.get_setup_params().get("tmp_base_dir") and not os.environ.get(
                    "TMP_BASE_DIR"
                ):
                    raise ValueError(
                        f"Program {program_name!r} has use_tmp=true but tmp_base_dir "
                        "is not set in config.toml [resource.setup] and TMP_BASE_DIR "
                        "is not in the environment"
                    )
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

    def get_docker_registry(self, resource: str | None = None) -> Optional[str]:
        """Optional registry host for ``docker pull`` (e.g. ``167.233.117.31:5000``).

        Used to rewrite ``docker.io/library/<image>`` to that registry. Unset
        means skip pull (local builds tagged as Docker Hub library names).
        """
        lookup = resource if resource is not None else self._resource
        value = self._resource_section(lookup).get("docker_registry")
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def get_program(self, program_name: str, resource: str | None = None) -> Dict[str, Any]:
        """
        Returns the dict from resource.program.name for program with name.
        Expected structure in TOML: [resource_name.program.program_name]

        If ``resource`` is omitted, uses the ResourceConfig's current resource.
        Resources may set ``same-as = "other-resource"`` to reuse that
        resource's program tables.

        Raises:
            ValueError: If the resource or ``[resource.program.name]`` block
                is missing from ``config.toml``.
        """
        lookup = resource if resource is not None else self._resource
        section = self._resource_section(lookup)
        programs = section.get("program")
        if programs is None:
            raise ValueError(
                f"config.toml resource {lookup!r} has no [program] table"
            )
        if not isinstance(programs, dict):
            raise ValueError(
                f"config.toml resource {lookup!r} [program] must be a table, "
                f"got {type(programs).__name__}"
            )
        if program_name not in programs:
            raise ValueError(
                f"config.toml has no [{lookup}.program.{program_name}] block"
            )
        program = programs[program_name]
        if not isinstance(program, dict):
            raise ValueError(
                f"config.toml [{lookup}.program.{program_name}] must be a "
                f"table, got {type(program).__name__}"
            )
        return program

    def get_setup_params(self) -> Dict[str, Any]:
        """
        Returns the setup dict for the specified resource.
        Expected structure in TOML: [resource_name.setup]
        """
        setup = self._resource_section(self._resource).get("setup")
        if setup is None:
            return {}
        return setup

    def get_postprocessing_params(self) -> Dict[str, Any]:
        """
        Returns the post-processing dict for the specified resource.
        Expected structure in TOML: [resource_name.post-processing] or [resource_name.postprocessing]
        """
        resource_cfg = self._resource_section(self._resource)
        if "post-processing" in resource_cfg:
            return resource_cfg["post-processing"]
        if "postprocessing" in resource_cfg:
            return resource_cfg["postprocessing"]
        return {}

    def __str__(self):
        return f"ResourceConfig(resource={self._resource}, config={self._config})"