import glob
import hashlib
import logging
import os
import subprocess
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Set
from pydantic import PrivateAttr

from simstack.models.document import ObjectId

from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.models.files import FileStack


local_logger = logging.getLogger("NodeRunner")
_LOG_TAIL_CHARS = 64 * 1024


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

    _last_stdout_text: str = PrivateAttr(default="")
    _last_stderr_text: str = PrivateAttr(default="")
    _last_stdout_slice: tuple[Path, int, int] | None = PrivateAttr(default=None)
    _last_stderr_slice: tuple[Path, int, int] | None = PrivateAttr(default=None)
    _node_log_path: Path | None = PrivateAttr(default=None)
    _node_log_snapshot_size: int = PrivateAttr(default=-1)

    @staticmethod
    def _read_output_slice(value: tuple[Path, int, int]) -> str:
        path, start, end = value
        with path.open("rb") as source:
            source.seek(start)
            return source.read(end - start).decode("utf-8", errors="replace")

    @property
    def last_stdout(self) -> str:
        """Full output remains available on demand; subprocess capture stays on disk."""
        return self._read_output_slice(self._last_stdout_slice) if self._last_stdout_slice else self._last_stdout_text

    @last_stdout.setter
    def last_stdout(self, value: str) -> None:
        self._last_stdout_slice = None
        self._last_stdout_text = value

    @property
    def last_stderr(self) -> str:
        return self._read_output_slice(self._last_stderr_slice) if self._last_stderr_slice else self._last_stderr_text

    @last_stderr.setter
    def last_stderr(self, value: str) -> None:
        self._last_stderr_slice = None
        self._last_stderr_text = value

    def __init__(
        self,
        name: str,
        task_id: str | ObjectId,
        logger: logging.Logger = None,
        **kwargs,
    ):
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
        self.last_returncode: int | None = None
        self.log_string = ""
        self.log_truncated = False
        self.info_file_patterns = {"*.in", "*.out", "*.err", "*.log"}
        self.custom_name = None
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
                        filepath = (
                            value if os.path.isabs(value) else str(search_cwd / value)
                        )
                        if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                            files_set.add(filepath)

            self.info(f"Processing patterns: {self.info_file_patterns} in {search_cwd}")

            # Add files matching patterns
            for pattern in self.info_file_patterns:
                # If pattern is not absolute, join it with search_cwd
                search_pattern = (
                    pattern if os.path.isabs(pattern) else str(search_cwd / pattern)
                )
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
        if self._node_log_path is None:
            from simstack.core.context import context

            root = self._task_workdir() if context.initialized else Path.cwd()
            self._node_log_path = root / f"node-{uuid.uuid4().hex}.log"
            with self._open_private_log(self._node_log_path):
                pass
        fd = os.open(self._node_log_path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "a", encoding="utf-8") as archive:
            archive.write(msg)
            archive.write("\n")
        tail = self.log_string + msg[-_LOG_TAIL_CHARS:] + "\n"
        self.log_truncated = self.log_truncated or len(msg) + len(self.log_string) + 1 > _LOG_TAIL_CHARS
        self.log_string = tail[-_LOG_TAIL_CHARS:]
        self.logger.debug(
            f"Task {self.name}: {msg[-_LOG_TAIL_CHARS:]} for task_id: {self.task_id}",
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

    def subprocess(self, name: str, command: List[str], cwd: str = "") -> bool:
        """
        Execute an explicit argv command and capture its output.

        This method runs a shell command, captures stdout and stderr, creates a log file
        with the execution details, and adds the log file to the info_files collection.

        Args:
            name (str): Name identifier for the subprocess (used for log file naming)
            command (List[str]): Executable and arguments; scripts require an explicit interpreter.
            cwd (str, optional): Working directory for command execution. Defaults to current directory.

        Returns:
            bool: True if the subprocess completed successfully (return code 0), False otherwise

        Note:
            - Creates a private, unique log for each invocation, including retries
            - Updates last_stdout and last_stderr attributes with the most recent output
            - Log file is automatically added to info_files collection
        """
        command_argv = self._validated_command(command)
        process_cwd = self._validated_process_cwd(cwd)
        log_path = self._private_process_log(name)

        with tempfile.TemporaryDirectory(prefix="process-output-", dir=self._task_workdir()) as capture:
            stdout_path, stderr_path = Path(capture) / "stdout", Path(capture) / "stderr"
            with self._open_private_log(stdout_path) as stdout, self._open_private_log(stderr_path) as stderr:
                process = subprocess.run(
                    command_argv, shell=False, stdout=stdout, stderr=stderr,
                    cwd=str(process_cwd), env=self._runner_subprocess_environment(), check=False,
                )
            self.info(f"run script {name} finished: {process.returncode}")
            self.last_returncode = process.returncode
            with self._open_private_log(log_path) as process_log:
                process_log.write(f"Command: {name or 'process'}\n{command_argv!r}\n")
                process_log.write(f"Process return code:\n{process.returncode}\n\n")
                process_log.write("Process output:\n")
                start = process_log.tell()
                with stdout_path.open(encoding="utf-8", errors="replace") as source:
                    shutil.copyfileobj(source, process_log, length=64 * 1024)
                self._last_stdout_slice = (log_path, start, process_log.tell())
                process_log.write("\n\nProcess error:\n")
                start = process_log.tell()
                with stderr_path.open(encoding="utf-8", errors="replace") as source:
                    shutil.copyfileobj(source, process_log, length=64 * 1024)
                self._last_stderr_slice = (log_path, start, process_log.tell())
                self._last_stdout_text = self._last_stderr_text = ""
                process_log.write("\n\n")
        file_stack = FileStack.from_local_file(
            log_path, in_memory=False, is_hashable=True, secure_source=True
        )
        self.info_files.append(file_stack)
        self.info(f"Subprocess '{name}' log added to info files: {file_stack.name}")
        return process.returncode == 0

    def submit_to_watchdog(self, name: str, command: List[str]) -> bool:
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
        task_workdir = self._validated_process_cwd("")
        queue_dir = task_workdir / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        command_argv = self._validated_command(command)
        log_path = self._private_process_log(name)
        from simstack.runner_runtime.watchdog import submit_to_watchdog

        try:
            with self._open_private_log(log_path) as process_log:
                process_log.write(f"Command: {name or 'process'}\n{command_argv!r}\n")

                job_id = str(self.task_id) + "_" + uuid.uuid4().hex
                result = submit_to_watchdog(
                    command_argv,
                    job_id,
                    queue_dir,
                    env=self._runner_subprocess_environment(),
                    cwd=str(task_workdir),
                )
                if result.status == "ok":
                    self.info(f"watchdog {result.returncode}")
                else:
                    err_msg = f"watchdog submission failed: {result.returncode}"
                    self.fail(err_msg)
                self.last_stdout = result.stdout
                self.last_stderr = result.stderr
                self.last_returncode = result.returncode
                process_log.write(f"Process return code:\n{result.returncode}\n\n")
                process_log.write(f"Process output:\n{result.stdout}\n\n")
                process_log.write(f"Process error:\n{result.stderr}\n\n")
            file_stack = FileStack.from_local_file(
                log_path, in_memory=True, is_hashable=True, secure_source=True
            )
            self.info_files.append(file_stack)
            self.info(
                f"Watchdog job '{name}' log added to info files: {file_stack.name}"
            )
            return result.status == "ok" and result.returncode == 0
        except Exception as e:
            err_msg = f"watchdog submission exception: {str(e)}"
            self.fail(err_msg)
        return False

    @staticmethod
    def _validated_command(command: str | List[str]) -> list[str]:
        if (
            isinstance(command, str)
            or not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError("NodeRunner commands must be non-empty argv lists")
        return list(command)

    def _task_workdir(self) -> Path:
        from simstack.core.context import context

        current = Path.cwd().resolve()
        if not context.runner_mode:
            return current
        from simstack.runner_runtime.config import runner_task_workdir

        expected = runner_task_workdir(
            Path(context.config.workdir), str(self.task_id)
        ).resolve()
        try:
            current.relative_to(expected)
        except ValueError as exc:
            raise RuntimeError(
                "NodeRunner process execution escaped its claimed task workdir"
            ) from exc
        return expected

    @property
    def task_workdir(self) -> Path:
        """Root owned by this task, including its subprocess scratch directories."""
        return self._task_workdir()

    def _validated_process_cwd(self, cwd: str) -> Path:
        task_workdir = self._task_workdir()
        selected = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        from simstack.core.context import context

        if context.runner_mode:
            try:
                selected.relative_to(task_workdir)
            except ValueError as exc:
                raise ValueError(
                    "NodeRunner subprocess cwd must stay in the claimed task workdir"
                ) from exc
        return selected

    def _private_process_log(self, name: str) -> Path:
        if not isinstance(name, str):
            raise ValueError("NodeRunner process name must be a string")
        component = hashlib.sha256((name or "process").encode("utf-8")).hexdigest()
        # Invocations and reclaimed attempts can share the same task workdir.
        # Keep old logs immutable without treating a repeated command as an error.
        return self._task_workdir() / f"process-{component}-{uuid.uuid4().hex}.log"

    @staticmethod
    def _open_private_log(path: Path):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def _runner_subprocess_environment(self) -> dict[str, str] | None:
        from simstack.core.context import context

        if not context.runner_mode:
            return None
        from simstack.runner_runtime.environment import (
            sanitized_workflow_subprocess_environment,
            worker_environment_allowlist_from_environment,
        )

        allowlist = worker_environment_allowlist_from_environment()
        return sanitized_workflow_subprocess_environment(allowlist)

    def _make_log_file(self):
        """
        Create a log file from the cumulative log string and add it to info_files.

        This internal method checks if `log_string` contains any messages. If it does,
        it creates a `FileStack` from the string and appends it to the `info_files` list,
        making it available as part of the task's results.
        """
        if self._node_log_path is not None:
            size = self._node_log_path.stat().st_size
            if size == self._node_log_snapshot_size:
                return
            snapshot = self._private_process_log("node-log-snapshot")
            with self._node_log_path.open(encoding="utf-8") as source, self._open_private_log(snapshot) as target:
                shutil.copyfileobj(source, target, length=64 * 1024)
            log_file = FileStack.from_local_file(snapshot, in_memory=False,
                                                is_hashable=True, secure_source=True)
            log_file.name = "log.txt"
            self.info_files.append(log_file)
            self._node_log_snapshot_size = size
            self.info(f"Log archive saved to info files as: {log_file.name}")
        elif len(self.log_string) > 0:
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
        self._make_log_file()
        self.info(f"succeeded {msg}")
        self.message = msg
        self.status = TaskStatus.COMPLETED
        return self
