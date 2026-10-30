"""Run an operator-configured program in the current API task allocation.

The Server supplies an immutable named invocation plan bound to the active
claim. The program executes inside this worker's existing local/Slurm/container
allocation, never on a supervisor host as a side effect of an HTTP request.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from simstack.models.files import FileStack, _strict_file_name
from simstack.runner_protocol.dto.programs import NamedProgramPlanV1
from simstack.runner_runtime.environment import (
    sanitized_workflow_subprocess_environment,
    validate_worker_environment_allowlist,
)


@dataclass(frozen=True)
class ProgramRunResult:
    output_files: tuple[FileStack, ...]
    stdout: FileStack
    stderr: FileStack
    returncode: int


class ProgramExecutionError(RuntimeError):
    def __init__(
        self, program: str, returncode: int, *, stdout: FileStack, stderr: FileStack
    ):
        super().__init__(
            f"Configured program {program!r} exited with code {returncode}"
        )
        self.returncode = returncode
        self.stdout, self.stderr = stdout, stderr


def invocation_argv(plan: NamedProgramPlanV1, arguments: Sequence[str]) -> list[str]:
    """Keep caller arguments out of shell source, including after setup scripts."""
    if isinstance(arguments, (str, bytes)):
        raise TypeError("arguments must be a sequence of strings, not str/bytes")
    if any(not isinstance(item, str) or "\x00" in item for item in arguments):
        raise ValueError("Program arguments must be a sequence of strings without NUL")
    if len(arguments) > 1024 or sum(len(item) for item in arguments) > 256 * 1024:
        raise ValueError("Program arguments exceed the invocation budget")
    command = plan.command
    if isinstance(command, str):
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
        uses_shell = (
            any(word and set(word) <= set(";&|<>()") for word in words)
            or any(
                character in command for character in ("$", "`", "\n", "*", "?", "~")
            )
            or (
                bool(words)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) is not None
            )
            or (
                bool(words)
                and words[0]
                in {"source", ".", "export", "ulimit", "eval", "set", "unset"}
            )
        )
        if not uses_shell:
            command = words
        elif arguments and re.search(r"\$(?:[@*1-9]|\{[@*1-9]\})", command) is None:
            raise ValueError(
                'A shell run_command receiving workflow arguments must explicitly use "$@" or positional parameters; use argv for automatic forwarding'
            )
    if isinstance(command, list) and not plan.scripts:
        return [*command, *arguments]
    shell = plan.shell or ["/bin/bash", "-c"]
    if os.name == "nt":
        raise RuntimeError(
            "Script-based named programs require a POSIX execution allocation; use argv on Windows"
        )
    if isinstance(command, list):
        script = "set -e\n" + "\n".join(plan.scripts) + '\nexec "$@"'
        return [*shell, script, "simstack-program", *command, *arguments]
    script = "set -e\n" + "\n".join([*plan.scripts, command])
    return [*shell, script, "simstack-program", *arguments]


async def _stop_process(process) -> None:
    from simstack.runner_runtime.processes import terminate_worker_tree

    await terminate_worker_tree(process)


def _stage_program_input(value: FileStack, source: Path, workdir: Path) -> Path:
    """Copy a verified FileStack materialization into the invocation directory.

    FileStack.get may return its existing resource cache instead of the requested
    directory. The named native command must still find its input by logical
    basename in its fresh cwd. Never overwrite another invocation entry.
    """
    source = Path(source)
    target = workdir / _strict_file_name(value.name)
    if source.is_symlink() or target.is_symlink():
        raise ValueError("Program input must be a regular materialized file")
    same_target = source.resolve() == target.resolve()
    if target.exists() and not same_target:
        raise FileExistsError(f"Program input target already exists: {value.name}")
    expected_size = value.blob.size_bytes if value.blob is not None else value.size
    expected_hash = (
        value.blob.checksum_sha256
        if value.blob is not None
        else value.hash
        if value.is_hashable
        else None
    )
    temporary = None
    try:
        with ExitStack() as files:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            incoming = files.enter_context(os.fdopen(descriptor, "rb"))
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Program input is not a regular file")
            output = None
            if not same_target:
                output_descriptor, path = tempfile.mkstemp(
                    prefix=".input-", dir=workdir
                )
                temporary = Path(path)
                output = files.enter_context(os.fdopen(output_descriptor, "wb"))
            digest, copied = hashlib.sha256(), 0
            while chunk := incoming.read(1024 * 1024):
                copied += len(chunk)
                if expected_size is not None and copied > expected_size:
                    raise ValueError("Program input exceeds immutable FileStack size")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            after = os.fstat(incoming.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("Program input changed during staging")
            if expected_size is not None and copied != expected_size:
                raise ValueError("Program input differs from immutable FileStack size")
            if (
                expected_hash is not None
                and digest.hexdigest() != expected_hash.lower()
            ):
                raise ValueError(
                    "Program input differs from immutable FileStack checksum"
                )
        if temporary is not None:
            # Atomic no-clobber publication on the invocation filesystem.
            os.link(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run_program(
    program: str,
    *,
    input_files: Sequence[FileStack] = (),
    output_names: Sequence[str] = (),
    arguments: Sequence[str] = (),
) -> ProgramRunResult:
    """Execute a named program under the current claim and return durable files.

    Select the task's resource/queue/container before calling this helper. It
    does not move a running task to another allocation. Configure run_command,
    scripts, shell and optional timeout_seconds in the same operator config.toml.
    Environment values come only from the worker's existing allowed environment.
    Native programs must remain in the worker-owned session and not daemonize.
    """
    from simstack.core.context import context
    from simstack.core.runner_execution.blobs import publish_runtime_blobs
    from simstack.core.runner_execution.state import current_runner_claim
    from simstack.runner_runtime.config import runner_task_workdir

    claim = current_runner_claim()
    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        raise RuntimeError(
            "Named-program execution requires a Linux task allocation for bounded process-tree cleanup"
        )
    if isinstance(output_names, (str, bytes)):
        raise TypeError("output_names must be a sequence of basenames")
    if isinstance(input_files, (str, bytes)):
        raise TypeError("input_files must be a sequence of FileStack objects")
    if isinstance(arguments, (str, bytes)):
        raise TypeError("arguments must be a sequence of strings, not str/bytes")
    response = await context.runner_api.get_program_plan(
        claim.task_id,
        claim_id=claim.claim_id,
        attempt=claim.attempt,
        program=program,
    )
    plan = response.plan
    names = [_strict_file_name(value) for value in output_names]
    inputs = list(input_files)
    if any(not isinstance(value, FileStack) for value in inputs):
        raise TypeError("Program inputs must be FileStack objects")
    input_names = [_strict_file_name(value.name) for value in inputs]
    if len(set(input_names)) != len(input_names) or len(set(names)) != len(names):
        raise ValueError("Program input/output names must be unique")
    reserved = {".program.stdout", ".program.stderr"}
    if set(names).intersection(input_names) or reserved.intersection(
        [*names, *input_names]
    ):
        raise ValueError("Program file names overlap inputs or reserved diagnostics")
    task_root = runner_task_workdir(Path(context.config.workdir), claim.task_id)
    task_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    programs_root = task_root / "programs"
    programs_root.mkdir(exist_ok=True, mode=0o700)
    if programs_root.is_symlink() or not programs_root.resolve().is_relative_to(
        task_root.resolve()
    ):
        raise ValueError("Program staging escapes the current task directory")
    workdir = Path(
        tempfile.mkdtemp(prefix=f"attempt-{claim.attempt}-", dir=programs_root)
    )
    for value in inputs:
        source = await asyncio.to_thread(value.get, workdir)
        await asyncio.to_thread(_stage_program_input, value, source, workdir)
    argv = invocation_argv(plan, arguments)
    lifecycle_wrapper = (
        Path(__file__).parents[1] / "runner_runtime" / "program_process.py"
    )
    argv = [sys.executable, "-I", str(lifecycle_wrapper), "--", *argv]
    environment = sanitized_workflow_subprocess_environment(
        validate_worker_environment_allowlist(plan.environment_allowlist)
    )
    stdout_path, stderr_path = workdir / ".program.stdout", workdir / ".program.stderr"
    process = None
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        try:
            # Own one process group inside the existing worker session. The
            # helper cleans it immediately; supervisor teardown fences all groups
            # in that worker-owned session on lease loss and worker exit.
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                process_group=0,
            )
            await asyncio.wait_for(process.wait(), timeout=plan.timeout_seconds)
        finally:
            if process is not None:
                await _stop_process(process)
    for path in (stdout_path, stderr_path):
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(workdir)
        ):
            raise ValueError("Program diagnostics escaped the invocation workdir")
    stdout_file = FileStack.from_local_file(
        stdout_path, in_memory=False, secure_source=True
    )
    stderr_file = FileStack.from_local_file(
        stderr_path, in_memory=False, secure_source=True
    )
    if process.returncode:
        await context.runner_api.get_program_plan(
            claim.task_id,
            claim_id=claim.claim_id,
            attempt=claim.attempt,
            program=program,
        )
        await asyncio.to_thread(publish_runtime_blobs, [stdout_file, stderr_file])
        raise ProgramExecutionError(
            program, process.returncode, stdout=stdout_file, stderr=stderr_file
        )
    outputs = []
    for name in names:
        path = workdir / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(workdir)
        ):
            raise ValueError(
                f"Configured program did not produce a regular output {name!r}"
            )
        outputs.append(
            FileStack.from_local_file(path, in_memory=False, secure_source=True)
        )
    result = ProgramRunResult(
        tuple(outputs), stdout_file, stderr_file, process.returncode
    )
    # Fence reads/outputs with the still-live API claim before returning them to
    # scientific code. Normal task commit subsequently attaches these references.
    await context.runner_api.get_program_plan(
        claim.task_id, claim_id=claim.claim_id, attempt=claim.attempt, program=program
    )
    await asyncio.to_thread(publish_runtime_blobs, [*outputs, stdout_file, stderr_file])
    return result
