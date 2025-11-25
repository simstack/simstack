import glob
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Set

from simstack.core.definitions import TaskStatus
from simstack.core.simstack_result import SimstackResult
from simstack.models.files import FileStack

local_logger = logging.getLogger("NodeRunner")


class NodeRunner(SimstackResult):
    """
    A task runner class that extends SimstackResult for executing subprocess commands and managing task execution.

    This class provides functionality for running shell commands, collecting output files, and managing
    task status with comprehensive logging capabilities.

    Attributes:
        task_id (ObjectId): Unique identifier for the task
        name (str): Name of the task runner instance
        logger: Logger instance for recording task activities
        last_stdout (str): Most recent stdout from subprocess execution
        last_stderr (str): Most recent stderr from subprocess execution
        info_file_patterns (Set[str]): File patterns used to collect information files
    """

    def __init__(self, name: str = "node", logger=None, **kwargs):
        """
        Initialize the NodeRunner instance.

        Args:
            name (str, optional): Name for this task runner. Defaults to "node".
            logger (optional): Logger instance to use. If None, uses local_logger.
            **kwargs: Additional keyword arguments. 'task_id' can be provided here.
        """
        super().__init__()
        self.task_id = kwargs.get("task_id", "NA")
        self.name = name
        self.logger = logger or local_logger
        self.last_stdout = ""
        self.last_stderr = ""
        self.info_file_patterns = {"*.in", "*.out", "*.err", "*.log"}
        self.info("started")

    async def make_info_files(self, *args):
        """
        Collect and process information files based on patterns and explicit file paths.

        This method processes the provided arguments to identify file patterns and explicit
        file paths, then collects all matching files into FileStack objects for later use.

        Args:
            *args: Variable arguments that can be:
                - File patterns (strings containing '*')
                - Explicit file paths (existing readable files)

        Note:
            Files are added to the info_files list as FileStack objects.
            Only readable files are processed.
        """
        try:
            files_set: Set[str] = set()

            # Process args for patterns and files
            for value in args:
                if isinstance(value, str):
                    if "*" in value:
                        self.info_file_patterns.add(value)
                    elif os.path.exists(value) and os.access(value, os.R_OK):
                        files_set.add(value)

            self.info(f"Processing patterns: {self.info_file_patterns}")

            # Add files matching patterns
            for pattern in self.info_file_patterns:
                for filepath in glob.glob(pattern):
                    if os.path.exists(filepath) and os.access(filepath, os.R_OK):
                        files_set.add(filepath)

            self.info(f"Found info files: {files_set}")

            for file_path in files_set:
                if os.path.exists(file_path):
                    file_stack = FileStack.from_local_file(
                        file_path, in_memory=True, is_hashable=True, secure_source=True
                    )
                    self.info_files.append(file_stack)
        except Exception as finally_error:
            self.error(f"Error in finally block: {str(finally_error)}")

    def debug(self, msg):
        """
        Log a debug message with task context.

        Args:
            msg (str): Debug message to log
        """
        self.logger.debug(f"Task {self.name}: {msg} for task_id: {self.task_id}")

    def info(self, msg):
        """
        Log an info message with the task context.

        Args:
            msg (str): Info message to log
        """
        self.logger.info(f"Task {self.name}: {msg} task_id: {self.task_id}")

    def warning(self, msg):
        """
        Log a warning message with the task context.

        Args:
            msg (str): Warning message to log
        """
        self.logger.warning(f"Task {self.name}: {msg} task_id: {self.task_id}")

    def error(self, msg):
        """
        Log an error message with task context.

        Args:
            msg (str): Error message to log
        """
        self.logger.error(f"Task {self.name}: {msg} task_id: {self.task_id}")

    def subprocess(self, name: str, command: str, cwd: str = "") -> bool:
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
        with open(f"{name}.log", "w") as process_log:
            process_log.write(f"Command: {name}\n{command}\n")
            # TODO adapt for docker
            process = subprocess.run(
                command,
                shell=True,  # Important: use shell=True for shell operators like &&
                capture_output=True,
                text=True,
                cwd=cwd if cwd else None,
            )
            self.info(f"run script {name} finished: {process.returncode}")
            self.last_stdout = process.stdout
            self.last_stderr = process.stderr
            process_log.write(f"Process return code:\n{process.returncode}\n\n")
            process_log.write(f"Process output:\n{process.stdout}\n\n")
            process_log.write(f"Process error:\n{process.stderr}\n\n")
        file_stack = FileStack.from_local_file(
            f"{name}.log", in_memory=True, is_hashable=True, secure_source=True
        )
        self.info_files.append(file_stack)
        return process.returncode == 0

    def submit_to_watchdog(self, name: str, command: str) -> bool:
        """
        Submit a command to an external watchdog process for execution.

        This method prepares the necessary files and environment to submit a command
        to a watchdog process that will handle its execution. It uses a specified queue directory
        to manage job files and signals.

        Args:
            name (str): Name identifier for the watchdog job (used for log file naming)
            command (str): Command to be executed by the watchdog
        Returns:
            SimstackResult: Self reference with status updated based on the submission outcome
        Note:
            - The method sets the task status to SUBMITTED if the submission is successful.
            - If the submission fails, it sets the status to FAILED and records an error message.
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
            return result.status == "ok" and result.returncode == 0
        except Exception as e:
            err_msg = f"watchdog submission exception: {str(e)}"
            self.fail(err_msg)
        return False

    def fail(self, msg: str) -> SimstackResult:
        """
        Mark the task as failed with an error message.

        Args:
            msg (str): Error message describing the failure reason

        Returns:
            SimstackResult: Self reference with status set to FAILED
        """
        self.logger.exception(f"Task {self.name}: {msg} task_id: {self.task_id}")
        self.error_message = msg
        self.status = TaskStatus.FAILED
        return self

    def succeed(self, msg: str = "") -> SimstackResult:
        """
        Mark the task as successfully completed.

        Args:
            msg (str, optional): Success message. Defaults to empty string.

        Returns:
            SimstackResult: Self reference with status set to COMPLETED
        """
        self.info(f"succeeded {msg}")
        self.message = msg
        self.status = TaskStatus.COMPLETED
        return self
