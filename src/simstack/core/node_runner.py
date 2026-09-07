import glob
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Set, List, Tuple, Union, Optional

from odmantic import ObjectId, Model

from simstack.core.definitions import TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.models.files import FileStack


local_logger = logging.getLogger("NodeRunner")


class NodeRunner(SimstackResult):
    """
    A task runner class that extends SimstackResult for executing subprocess commands and managing task execution.

    This class provides functionality for running shell commands, collecting output files, and managing
    task status with comprehensive logging capabilities. It acts as a bridge between the Simstack node
    execution environment and external processes, ensuring that output, errors, and informational files
    are correctly captured and associated with the task.

    Attributes:
        task_id (str): Unique identifier for the task, typically a string representation of an ObjectId.
        name (str): Name of the task runner instance, usually derived from the node function name.
        logger (logging.Logger): Logger instance for recording task activities.
        last_stdout (str): Most recent stdout from subprocess execution.
        last_stderr (str): Most recent stderr from subprocess execution.
        log_string (str): Cumulative log of messages recorded via the `log` method.
        info_file_patterns (Set[str]): Set of file patterns (e.g., "*.log") used to collect information files.
    """

    def __init__(self, name: str, task_id: str | ObjectId, logger: logging.Logger = None, **kwargs):
        """
        Initialize the NodeRunner instance.

        Args:
            name (str): Name for this task runner.
            task_id (str | ObjectId): Unique identifier for the task.
            logger (logging.Logger, optional): Logger instance to use. If None, uses a logger named 'NodeRunner'.
            kwargs: Additional keyword arguments.
        """
        super().__init__()
        self.task_id = str(task_id) if isinstance(task_id, ObjectId) else task_id
        self.name = name
        self.logger = logger or local_logger
        self.last_stdout = ""
        self.last_stderr = ""
        self.log_string = ""
        self.info_file_patterns = {"*.in", "*.out", "*.err", "*.log"}
        self.custom_name = None
        self.scratch_dir = None
        self._node_dir = None
        self._program_name = None
        self._file_watchdog_proc = None
        self.info(f"NodeRunner '{self.name}' initialized for task_id: {self.task_id}")


    @classmethod
    def from_kwargs(cls, **kwargs) -> "NodeRunner":
        """
        Create a NodeRunner instance from keyword arguments.

        If 'node_runner' is already present in kwargs and is an instance of NodeRunner, it returns it.
        Otherwise, it initializes a new NodeRunner using 'name', 'task_id', and optionally 'logger' from kwargs.

        Args:
            **kwargs: Keyword arguments containing 'node_runner' or initialization parameters.

        Returns:
            NodeRunner: An existing or newly created NodeRunner instance.
        """
        node_runner = kwargs.get("node_runner")
        if isinstance(node_runner, cls):
            return node_runner

        return cls(
            name=kwargs["name"],
            task_id=kwargs["task_id"],
            logger=kwargs.get("logger"),
        )

    def _require_resource_config(self):
        from simstack.core.context import context

        if not context.initialized:
            raise ValueError("context is not initialized")
        resource_config = context.resource_config
        if resource_config is None:
            raise ValueError("context.resource_config is not available")
        return resource_config

    def _stop_file_watchdog(self) -> None:
        proc = self._file_watchdog_proc
        if proc is None:
            return
        self._file_watchdog_proc = None
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def stage(
        self,
        input_files: Optional[List[Union[str, FileStack]]] = None,
    ) -> Path:
        """Create the ResourceConfig scratch directory and copy input files into it.

        The Python process stays in the node directory. ``execute`` runs with
        ``cwd`` set to the returned scratch path.

        Args:
            input_files: Files to copy into scratch. Strings are paths relative
                to the node directory. ``FileStack`` values are materialized with
                ``.get(local_dir=scratch)``. Omitted means copy nothing.

        Returns:
            The scratch directory path.

        Raises:
            ValueError: Already staged, context/resource config missing,
                ``tmp_base_dir`` is not configured, or a listed input file is missing.
        """
        if self.scratch_dir is not None:
            raise ValueError("scratch is already staged")
        resource_config = self._require_resource_config()
        if not resource_config.get_setup_params().get("tmp_base_dir") and not os.environ.get(
            "TMP_BASE_DIR"
        ):
            raise ValueError(
                "tmp_base_dir is not set in config.toml [resource.setup] and "
                "TMP_BASE_DIR is not in the environment"
            )
        self._node_dir = Path.cwd()
        scratch = resource_config.tmp_dir(self.task_id)
        self.scratch_dir = Path(scratch)
        self.info(f"Writing temporary files to scratch directory: {self.scratch_dir}")
        if input_files:
            for item in input_files:
                if hasattr(item, "get"):
                    item.get(local_dir=self.scratch_dir)
                    continue
                src = self._node_dir / item
                if not src.exists():
                    raise ValueError(
                        f"Input file {item!r} does not exist in {self._node_dir}"
                    )
                dest = self.scratch_dir / Path(item).name
                if src.resolve() != dest.resolve():
                    shutil.copy2(src, dest)
        return self.scratch_dir

    def execute(self, program_name: str) -> bool:
        """Run ``run_command`` for ``program_name`` from ResourceConfig.

        Executes in the staged scratch directory when ``stage`` was called,
        otherwise in the current working directory.

        Args:
            program_name: Program key under ``[<resource>.program.<name>]``.

        Returns:
            True if the subprocess returned 0, False otherwise.

        Raises:
            ValueError: Context/resource config missing, program not found, or
                ``run_command`` is missing/empty.
        """
        if not program_name:
            raise ValueError("program_name is required")
        resource_config = self._require_resource_config()
        params = resource_config.get_program(program_name)
        if not params:
            raise ValueError(f"Program {program_name!r} not found in ResourceConfig")
        run_command = params.get("run_command")
        if not run_command:
            raise ValueError(f"Program {program_name!r} has no run_command")
        self._program_name = program_name
        cwd = str(self.scratch_dir) if self.scratch_dir is not None else ""
        return self.subprocess(program_name, run_command, cwd=cwd)

    def retrieve(
        self,
        output_files: Optional[List[Union[str, FileStack]]] = None,
    ) -> None:
        """Copy result files from scratch back to the node directory.

        Stops the file watchdog. If ``output_files`` is a list (including empty),
        copies those names only. If omitted, copies every item in scratch.
        Deletes scratch only when ``scratch_cleanup`` is a bool in the program
        or post-processing config.

        Raises:
            ValueError: ``stage`` was never called, or ``scratch_cleanup`` is set
                but is not a bool.
        """
        if self.scratch_dir is None or self._node_dir is None:
            raise ValueError("retrieve requires stage() first")
        self._stop_file_watchdog()
        scratch = Path(self.scratch_dir)
        workdir = Path(self._node_dir)
        try:
            if scratch.exists() and scratch.resolve() != workdir.resolve():
                if output_files is None:
                    items = list(scratch.iterdir())
                else:
                    items = []
                    for item in output_files:
                        name = item.name if hasattr(item, "name") else item
                        items.append(scratch / name)
                for item in items:
                    if not item.exists():
                        continue
                    dest = workdir / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dest)
            cleanup = False
            resource_config = self._require_resource_config()
            program_params = {}
            if self._program_name:
                program_params = resource_config.get_program(self._program_name) or {}
            post_params = resource_config.get_postprocessing_params() or {}
            if "scratch_cleanup" in program_params:
                flag = program_params["scratch_cleanup"]
                if not isinstance(flag, bool):
                    raise ValueError(
                        f"scratch_cleanup for program {self._program_name!r} must be a bool, "
                        f"got {flag!r}"
                    )
                cleanup = flag
            elif "scratch_cleanup" in post_params:
                flag = post_params["scratch_cleanup"]
                if not isinstance(flag, bool):
                    raise ValueError(
                        f"post-processing scratch_cleanup must be a bool, got {flag!r}"
                    )
                cleanup = flag
            if cleanup and scratch.exists() and scratch.resolve() != workdir.resolve():
                shutil.rmtree(scratch)
        finally:
            self.scratch_dir = None
            self._node_dir = None
            self._program_name = None

    def file_watchdog(self, files: List[str], interval: float) -> None:
        """Copy ``files`` from scratch to the node directory every ``interval`` seconds.

        Starts a child process (not a thread) so copying does not hold the GIL.
        The child exits when this process dies (parent-pid poll; Linux also uses
        ``PR_SET_PDEATHSIG``). Stopped by ``retrieve``, ``fail``, and ``succeed``.

        Args:
            files: Non-empty list of file names relative to scratch.
            interval: Positive number of seconds between copy passes.

        Raises:
            ValueError: Scratch was not staged, files/interval are invalid, or a
                watchdog is already running.
        """
        if self.scratch_dir is None or self._node_dir is None:
            raise ValueError("file_watchdog requires stage() first")
        if self._file_watchdog_proc is not None and self._file_watchdog_proc.poll() is None:
            raise ValueError("file_watchdog is already running")
        if not files:
            raise ValueError("files must be a non-empty list")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError(f"interval must be a positive number, got {interval!r}")
        cmd = [
            sys.executable,
            "-m",
            "simstack.util.scratch_file_watchdog",
            "--scratch",
            str(self.scratch_dir),
            "--dest",
            str(self._node_dir),
            "--interval",
            str(interval),
            "--parent-pid",
            str(os.getpid()),
            *files,
        ]
        env = os.environ.copy()
        import simstack

        src_dir = str(Path(simstack.__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir if not existing else src_dir + os.pathsep + existing
        self._file_watchdog_proc = subprocess.Popen(cmd, env=env)
        self.info(
            f"Started file watchdog for {files} every {interval}s "
            f"(pid {self._file_watchdog_proc.pid})"
        )

    async def make_info_files(self, *args, cwd: str | Path = ""):
        """
        Collect and process information files based on patterns and explicit file paths.

        This method processes the provided arguments to identify file patterns and explicit
        file paths, then collects all matching files into FileStack objects for later use.

        Args:
            *args: Variable arguments that can be:
                - File patterns (strings containing '*')
                - Explicit file paths (existing readable files)
            cwd (str | Path, optional): The directory where to look for files.
                Defaults to current directory.

        Note:
            Files are added to the info_files list as FileStack objects.
            Only readable files are processed.
        """
        try:
            if not cwd:
                search_cwd = Path.cwd()
            else:
                search_cwd = Path(cwd)

            files_set: Set[str] = set()
            # Process args for patterns and files
            for value in args:
                if isinstance(value, str):
                    if "*" in value:
                        self.info_file_patterns.add(value)
                    else:
                        filepath = value if os.path.isabs(value) else str(search_cwd / value)
                        if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                            files_set.add(filepath)

            self.info(f"Processing patterns: {self.info_file_patterns} in {search_cwd}")

            # Add files matching patterns
            for pattern in self.info_file_patterns:
                # If pattern is not absolute, join it with search_cwd
                search_pattern = pattern if os.path.isabs(pattern) else str(search_cwd / pattern)
                for filepath in glob.glob(search_pattern):
                    if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                        files_set.add(filepath)

            self.info(f"Found info files: {files_set}")

            for file_path in files_set:
                if os.path.exists(file_path):
                    file_stack = FileStack.from_local_file(
                        file_path, in_memory=True, is_hashable=True, secure_source=True
                    )
                    self.info_files.append(file_stack)
                    self.info(f"Info file added: {file_path}")
        except Exception as e:
            self.error(f"Error in make_info_files: {str(e)}")
            raise RuntimeError(f"Error in make_info_files: {str(e)}") from e

    def debug(self, msg):
        """
        Log a debug message with task context.

        Args:
            msg (str): Debug message to log
        """
        self.logger.debug(
            f"Task {self.name}: {msg} for task_id: {self.task_id}",
            stacklevel=2,
        )

    def log(self, msg: str):
        """
        Log a message and append it to the cumulative log string.

        This method records a message in the internal `log_string` and also logs it
        as a debug message using the logger. The cumulative log string can later
        be saved as a log file when the task finishes.

        Args:
            msg (str): Message to log.
        """
        self.log_string += f"{msg}\n"
        self.logger.debug(
            f"Task {self.name}: {msg} for task_id: {self.task_id}",
            stacklevel=2,
        )

    def info(self, msg):
        """
        Log an info message with task context.

        Args:
            msg (str): Info message to log.
        """
        self.logger.info(
            f"Task {self.name}: {msg} task_id: {self.task_id}",
            stacklevel=2,
        )

    def warning(self, msg):
        """
        Log a warning message with task context.

        Args:
            msg (str): Warning message to log.
        """
        self.logger.warning(
            f"Task {self.name}: {msg} task_id: {self.task_id}",
            stacklevel=2,
        )

    def error(self, msg):
        """
        Log an error message with task context and exception info.

        Args:
            msg (str): Error message to log.
        """
        self.logger.error(
            f"Task {self.name}: {msg} task_id: {self.task_id}",
            stacklevel=2,
            exc_info=True,
        )

    def subprocess(self, name: str, command: str | List[str], cwd: str = "") -> bool:
        """
        Execute a shell command as a subprocess and capture its output.

        This method runs a shell command, captures stdout and stderr, creates a log file
        with the execution details, and adds the log file to the info_files collection.

        Args:
            name (str): Name identifier for the subprocess (used for log file naming)
            command (str): Shell command to execute
            cwd (str, optional): Working directory for command execution. Defaults to current directory.

        Returns:
            bool: True if the subprocess completed successfully (return code 0), False otherwise

        Note:
            - Creates a log file named "{name}.log" containing command details and output
            - Updates last_stdout and last_stderr attributes with the most recent output
            - Log file is automatically added to info_files collection
        """
        if name == "":
            name = "process"
        if isinstance(command, list):
            command = " ".join(command)

        exec_cwd = cwd if cwd else (str(self.scratch_dir) if self.scratch_dir else "")
        if exec_cwd:
            Path(exec_cwd).mkdir(parents=True, exist_ok=True)
        log_path = str(Path(exec_cwd) / f"{name}.log") if exec_cwd else f"{name}.log"

        with open(log_path, "w", encoding="utf-8") as process_log:
            process_log.write(f"Command: {name}\n{command}\n")
            # TODO adapt for docker
            process = subprocess.run(
                command,
                shell=True,  # Important: use shell=True for shell operators like &&
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=exec_cwd if exec_cwd else None,
            )
            self.info(f"run script {name} finished: {process.returncode}")
            self.last_stdout = process.stdout
            self.last_stderr = process.stderr
            process_log.write(f"Process return code:\n{process.returncode}\n\n")
            process_log.write(f"Process output:\n{process.stdout}\n\n")
            process_log.write(f"Process error:\n{process.stderr}\n\n")
        file_stack = FileStack.from_local_file(
            log_path, in_memory=True, is_hashable=True, secure_source=True
        )
        self.info_files.append(file_stack)
        self.info(f"Subprocess '{name}' log added to info files: {file_stack.name}")
        return process.returncode == 0

    def submit_to_watchdog(self, name: str, command: str) -> bool:
        """
        Submit a command to an external watchdog process for execution.

        This method prepares the necessary files and environment to submit a command
        to a watchdog process that will handle its execution. It uses a specified queue directory
        to manage job files and signals.

        Args:
            name (str): Name identifier for the watchdog job (used for log file naming).
            command (str): Command to be executed by the watchdog.

        Returns:
            bool: True if the submission and execution were successful, False otherwise.

        Note:
            - The method sets the task status to FAILED if the submission or execution fails.
            - Job logs are automatically added to the info_files collection.
        """
        # we are inside a slurm job, so we need to use the queue dir of the job
        queue_dir = Path.cwd() / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        if name == "":
            name = "process"
        from simstack.util.submit_to_watchdog import submit_to_watchdog

        try:
            with open(f"{name}.log", "w") as process_log:
                process_log.write(f"Command: {name}\n{command}\n")

                job_id = str(self.task_id) + "_" + uuid.uuid4().hex
                result = submit_to_watchdog(command, job_id, queue_dir)
                if result.status == "ok":
                    self.info(f"watchdog {result.exit_code}")
                else:
                    err_msg = f"watchdog submission failed: {result.returncode}"
                    self.fail(err_msg)
                self.last_stdout = result.stdout
                self.last_stderr = result.stderr
                process_log.write(f"Process return code:\n{result.exit_code}\n\n")
                process_log.write(f"Process output:\n{result.stdout}\n\n")
                process_log.write(f"Process error:\n{result.stderr}\n\n")
            file_stack = FileStack.from_local_file(
                f"{name}.log", in_memory=True, is_hashable=True, secure_source=True
            )
            self.info_files.append(file_stack)
            self.info(f"Watchdog job '{name}' log added to info files: {file_stack.name}")
            return result.status == "ok" and result.returncode == 0
        except Exception as e:
            err_msg = f"watchdog submission exception: {str(e)}"
            self.fail(err_msg)
        return False

    def _make_log_file(self):
        """
        Create a log file from the cumulative log string and add it to info_files.

        This internal method checks if `log_string` contains any messages. If it does,
        it creates a `FileStack` from the string and appends it to the `info_files` list,
        making it available as part of the task's results.
        """
        if len(self.log_string) > 0:
            log_file = FileStack.from_string(self.log_string, file_name="log.txt")
            self.info_files.append(log_file)
            self.info(f"Log string saved to info files as: {log_file.name}")

    def fail(self, msg: str) -> "NodeRunner":
        """
        Mark the task as failed and record an error message.

        This method updates the task status to FAILED, sets the error message,
        logs the error, and ensures the cumulative log file is created.

        Args:
            msg (str): Error message describing the failure.

        Returns:
            NodeRunner: Self reference for chaining.
        """
        self._stop_file_watchdog()
        self._make_log_file()
        self.logger.error(
            f"Task {self.name}: {msg} task_id: {self.task_id}",
            stacklevel=2,
            exc_info=True,
        )
        self.error_message = msg
        self.status = TaskStatus.FAILED
        return self

    def succeed(self, msg: str = "") -> "NodeRunner":
        """
        Mark the task as successfully completed.

        This method updates the task status to COMPLETED, sets an optional
        success message, logs the success, and ensures the cumulative log file is created.

        Args:
            msg (str, optional): Success message. Defaults to "".

        Returns:
            NodeRunner: Self reference for chaining.
        """
        self._stop_file_watchdog()
        self._make_log_file()
        self.info(f"succeeded {msg}")
        self.message = msg
        self.status = TaskStatus.COMPLETED
        return self
