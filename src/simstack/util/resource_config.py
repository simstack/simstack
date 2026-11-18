import copy
import tomllib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

from simstack.models.document import ObjectId

import logging
logger = logging.getLogger("ResourceConfig")


# Site configs use POSIX path expressions, including ${TMPDIR:-/tmp/${USER}}.
_SCRATCH_VAR = re.compile(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?::-((?:[^{}]|\$\{\w+\})*))?\})")


def _expand_scratch_word(word: str, *, condition: bool = False) -> str:
    def variable(match):
        name, braced, fallback = match.groups()
        value = os.environ.get(name or braced)
        if not value and fallback is not None:
            return _expand_scratch_word(fallback, condition=condition)
        return value if value is not None else ("" if condition else match[0])

    return _SCRATCH_VAR.sub(variable, word)


def _scratch_condition(condition: str) -> bool:
    # Site selectors use POSIX test operands (-a, -e, -d, !=, ...) and &&.
    # Each expanded operand is passed as data, including paths containing spaces.
    words = shlex.split(condition)
    while words:
        end = words.index("&&") if "&&" in words else len(words)
        clause, words = words[:end], words[end + 1:]
        if clause[:1] == ["["] and clause[-1:] == ["]"]:
            operands = clause[1:-1]
        elif clause[:1] == ["test"]:
            operands = clause[1:]
        else:
            raise ValueError("Expected a test condition in tmp_base_dir")
        args = [_expand_scratch_word(word, condition=True) for word in operands]
        status = subprocess.run(["test", *args], env={"PATH": os.defpath}).returncode
        if status not in (0, 1):
            raise ValueError("Invalid test expression in tmp_base_dir")
        if status == 1:
            return False
    return True


def _configured_scratch(text: str) -> str:
    # Evaluate only the site config's test and selected path assignment. The
    # config may come from a workflow repository and is never a shell command.
    text = "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")).strip()
    # Some site definitions print the selection as a final diagnostic.
    text = re.sub(r"(?:;|\n)\s*echo\s+[^;\n]+$", "", text).strip()
    conditional = re.match(r"if\s", text)
    if conditional:
        choice = re.fullmatch(
            r"if\s+([^;\n]+)[;\n]\s*then\s+(.+?)[;\n]\s*"
            r"(?:else\s+(.+?)[;\n]\s*)?fi",
            text, re.DOTALL,
        )
        if not choice:
            raise ValueError("Unsupported tmp_base_dir conditional")
        condition, when_true, when_false = choice.groups()
        text = when_true if _scratch_condition(condition) else when_false
        if text is None:
            return ""

    assignment = re.fullmatch(r"(?:set\s+|export\s+)?TMP_BASE_DIR\s*=\s*([^\n]*)", text.strip(), re.I)
    if conditional and not assignment:
        raise ValueError("Expected a TMP_BASE_DIR assignment in the selected branch")
    value = (assignment[1] if assignment else text).strip()
    if "\n" in value:
        raise ValueError("Unsupported tmp_base_dir assignment")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return _expand_scratch_word(value)


def resolve_tmp_base_dir(configured: str | None) -> Path:
    """Resolve the site's scratch selection in the current host environment."""
    value = _configured_scratch(str(configured or "").strip())
    last_error = None
    for raw in (value, _expand_scratch_word(os.environ.get("TMP_BASE_DIR", ""))):
        expanded = os.path.expanduser(raw)
        if os.name == "nt":
            expanded = os.path.expandvars(expanded)
        if not expanded or "$" in expanded or (os.name == "nt" and "%" in expanded):
            continue
        path = Path(expanded)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            last_error = exc
            continue
        return path
    if configured:
        raise ValueError(f"Could not resolve tmp_base_dir from {configured!r}") from last_error
    return Path(tempfile.gettempdir())


def slurm_task_scratch(task_id: ObjectId | str) -> Path | None:
    """The directory selected by the batch wrapper for this task or array member."""
    value = os.environ.get("SIMSTACK_SLURM_TASK_SCRATCH")
    if value:
        path = Path(value)
        if path.is_absolute() and path.name == str(task_id):
            return path
    return None


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
        # The batch wrapper and calculation must keep using the same directory
        # even if a program changes TMPDIR. Nested tasks have their own IDs.
        tmp_dir = slurm_task_scratch(task_id) or self.tmp_base_dir / str(task_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return tmp_dir

    @property
    def tmp_base_dir(self) -> Path:
        return resolve_tmp_base_dir(self.get_setup_params().get("tmp_base_dir"))

    def run(self,
        program_name: str,
        input_files: Optional[List[Union[str, "FileStack"]]] = None,
        output_files: Optional[List[Union[str, "FileStack"]]] = None,
        node_runner: Optional[Any] = None,
        command: str = "run_command",
        name: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Executes a command from the program table with optional temporary
        directory usage and file handling.

        Args:
            program_name: Name of the program to run.
            input_files: List of input files (str or FileStack). Overrides TOML input_files if provided.
            output_files: List of output files (str or FileStack). Overrides TOML output_files if provided.
            node_runner: Optional NodeRunner instance for execution.
            command: Key in ``[<resource>.program.<name>]`` whose string is
                executed. Defaults to ``run_command``. Use e.g.
                ``define_command`` for a second launcher on the same table.
            env: Environment overrides for this subprocess only.
            name: Subprocess log name for NodeRunner. Defaults to ``"run"``.

        Returns:
            True if the subprocess returned 0, False otherwise. Without a
            node_runner, ``subprocess.run(..., check=True)`` raises on failure
            and this method returns True.

        Raises:
            ValueError: ``command`` is empty, or the program table is missing
                that key or the value is not a non-empty string.
        """
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"command must be a non-empty string, got {command!r}")

        # Overrides belong to this invocation, never the global process.
        env_kwargs = {"env": env} if env is not None else {}
        params = self.get_program(program_name)
        if command not in params:
            raise ValueError(
                f"config.toml [{self._resource}.program.{program_name}] "
                f"is missing {command!r}"
            )
        run_command = params[command]
        if not isinstance(run_command, str) or not run_command.strip():
            raise ValueError(
                f"config.toml [{self._resource}.program.{program_name}] "
                f"{command!r} must be a non-empty string, got {run_command!r}"
            )

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

        subprocess_name = "run" if name is None else name
        if node_runner is not None:
            from simstack.core.node_runner import NodeRunner

            if isinstance(node_runner, NodeRunner):
                if use_temp:
                    node_runner.stage(input_files=input_files, resource_config=self)
                    ok = node_runner.execute(
                        program_name, command=command, name=subprocess_name,
                        resource_config=self, **env_kwargs
                    )
                    node_runner.retrieve(output_files=output_files, resource_config=self)
                    return ok
                return node_runner.execute(
                    program_name, command=command, name=subprocess_name,
                    resource_config=self, **env_kwargs
                )

        # scratch_cleanup from postprocessing
        post_params = self.get_postprocessing_params()
        scratch_cleanup = params.get("scratch_cleanup", post_params.get("scratch_cleanup", False))

        ok = True
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

            if node_runner and hasattr(node_runner, "subprocess"):
                ok = bool(
                    node_runner.subprocess(
                        subprocess_name, run_command, cwd=str(exec_dir), **env_kwargs
                    )
                )
            else:
                subprocess.run(
                    run_command, shell=True, check=True, cwd=exec_dir,
                    **({"env": {**os.environ, **env}} if env is not None else {}),
                )
                ok = True

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
        return ok

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
