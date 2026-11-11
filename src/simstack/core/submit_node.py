"""Generate a batch wrapper and submit one fenced execution to Slurm."""

import logging
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

from simstack.core.context import context
from simstack.core.node import process_is_in_docker
from simstack.util.sanitized_output import sanitized_tail

logger = logging.getLogger("submit_node")


def _write_launch_environment(path, environment):
    """Keep launcher identity independent of Slurm's user export policy."""
    names = {
        "SIMSTACK_SERVER_URL", "SIMSTACK_RUNNER_TOKEN", "SIMSTACK_SERVER_TOKEN",
        "SIMSTACK_RUNNER_CREDENTIALS", "SIMSTACK_LOCAL_EXECUTION_TOKEN",
        "SIMSTACK_REPOSITORY_REVISION", "SIMSTACK_MACHINE_CONFIG",
        "SIMSTACK_RUNNER_WORKDIR",
        "SIMSTACK_EXECUTION_TASK", "SIMSTACK_EXECUTION_CLAIM",
        "SIMSTACK_EXECUTION_START", "SIMSTACK_EXECUTION_GENERATION",
        "SIMSTACK_EXECUTION_RESOURCE", "SIMSTACK_EXECUTION_ACCOUNT_ID",
        "SIMSTACK_SLURM_SUBMISSION", "SIMSTACK_SLURM_SUBMISSION_TASK",
    }
    values = {key: value for key, value in environment.items() if key in names}
    # The batch script may be readable by others; credentials remain private.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("unset SIMSTACK_USER_TOKEN " + " ".join(sorted(names)) + "\n")
        for key, value in sorted(values.items()):
            stream.write(f"export {key}={shlex.quote(value)}\n")


async def _persist_submission_failure(entry, error):
    from simstack.runner_runtime.pending_execution_failures import record_failure

    error = sanitized_tail(error, None)
    record_failure(
        error, phase="submit", execution=context.server.tasks.execution(entry), server=context.server,
        resource=getattr(entry, "_execution_resource", None)
    )
    await context.server.tasks.fail_attempt(entry, error, phase="submit")


def make_executable(path):
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def submit_node(entry):
    from simstack.runner_runtime.slurm_submission_intents import (
        begin_intent, forget_intent, process_identity, update_intent,
    )

    if process_is_in_docker():
        error = "Slurm submission must be performed by the host Runner"
        await _persist_submission_failure(entry, error)
        raise RuntimeError(error)
    submitted = False
    submission_id = None
    try:
        from simstack.runner_runtime.execution_environment import (
            task_project_root,
            task_resource_config,
            task_environment,
            task_launch_command,
        )

        base = task_project_root(entry, context.config.project_root)
        config = task_resource_config(entry, context)
        resource = str(entry.parameters.resource)
        resource = str(context.config.resource) if resource == "self" else resource
        try:
            program = config.get_program(entry.name, resource=resource) or {}
        except ValueError:
            program = {}
        if entry.parameters.in_docker and not program.get("docker_image"):
            raise ValueError(
                f"Docker image for {entry.name} not found (resource={resource})"
            )
        if (
            "use_tmp" in program
            and "use_temp" in program
            and bool(program["use_tmp"]) != bool(program["use_temp"])
        ):
            raise ValueError("Conflicting use_tmp and use_temp configuration")
        use_tmp = program.get("use_tmp", program.get("use_temp", False))
        params = entry.parameters.slurm_parameters
        if params is None:
            raise ValueError(f"Task {entry.id} has no Slurm parameters")
        params = params.model_copy(deep=True)
        if os.environ.get("SBATCH_ARRAY_INX"):
            raise ValueError("Slurm job arrays are unsupported; use explicit child tasks or MassRunner")
        workdir = Path(context.config.workdir) / entry.name / str(entry.id)
        workdir.mkdir(parents=True, exist_ok=True)
        checkpoint_args = []
        if use_tmp:
            scratch = config.tmp_dir(entry.id)
            if not scratch.is_dir():
                raise ValueError(f"Scratch directory does not exist: {scratch}")
            destination = workdir / "recovery"
            checkpoint_script = (
                Path(__file__).parents[1] / "static" / "slurm_checkpoint.sh"
            )
            checkpoint_args = [
                "bash",
                str(checkpoint_script),
                str(scratch),
                str(destination),
            ]
            if params.signal is None:
                params.signal = "B:SIGUSR1@60"
        paths = list(dict.fromkeys([str(base), *map(str, context.config.python_paths)]))
        params.output = str(workdir / "%j.out")
        params.error = str(workdir / "%j.err")
        params.chdir = str(workdir)
        params.startup_commands.append("source ~/.bashrc")
        if context.config.environment_start:
            params.startup_commands.append(context.config.environment_start)
        params.startup_commands.append(
            "export PYTHONPATH="
            + shlex.quote(os.pathsep.join(paths))
            + ":${PYTHONPATH:-}"
        )
        command = task_launch_command(entry, resource, base)
        script = workdir / "slurm_script.sh"
        intent = begin_intent(context.server, entry)
        submission = await context.server.slurm.scheduler_submission(entry, intent["submission_id"])
        submission_id = submission["submission_id"]
        if submission_id != intent["submission_id"]:
            # Another owner already advanced this launch. Its actual job is
            # discovered through the scheduler, never by a second sbatch.
            update_intent(context.server, intent, submission_id=submission_id, phase="submitting")
            return True
        if intent["phase"] == "accepted":
            await context.server.slurm.scheduler_accepted(context.server.tasks.execution(entry),
                submission_id, intent["job_id"])
            forget_intent(context.server, intent)
            return True
        if intent["phase"] == "submitting":
            return True
        params.job_name = "ss-" + submission_id
        environment = task_environment(entry, server=context.server)
        environment["SIMSTACK_SLURM_SUBMISSION"] = submission_id
        environment["SIMSTACK_SLURM_SUBMISSION_TASK"] = str(entry.id)
        launch_environment = workdir / ("slurm_environment_" + submission_id + ".sh")
        _write_launch_environment(launch_environment, environment)
        params.startup_commands.append("source " + shlex.quote(str(launch_environment)) + " || exit $?")
        params.startup_commands.append(
            "exec " + shlex.join([*checkpoint_args, *command])
        )
        script.write_text(params.to_sbatch_header())
        make_executable(script)
        # Phase replay is safe while the local journal still says prepared:
        # no external process has been created yet.
        await context.server.slurm.scheduler_submission(entry, submission_id, phase="submitting")
        intent = update_intent(context.server, intent, phase="submitting",
            process_id=os.getpid(), process_birth=process_identity(os.getpid()))
        submitted = True
        try:
            process = subprocess.Popen(
                # Slurm gives environment options precedence over #SBATCH
                # directives. The CLI must own our reconciliation identity.
                ["sbatch", "--parsable", "--job-name=" + params.job_name, str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except OSError:
            submitted = False  # The operating system did not start sbatch.
            raise
        intent = update_intent(context.server, intent, process_id=process.pid,
            process_birth=process_identity(process.pid))
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        stdout = sanitized_tail(stdout, None).strip()
        stderr = sanitized_tail(stderr, None)
        if process.returncode:
            await _persist_submission_failure(
                entry, f"sbatch failed with exit {process.returncode}: {stderr}"
            )
            forget_intent(context.server, intent)
            return False
        match = re.fullmatch(r"(\d+)(?:;[^\s]+)?", stdout)
        if match is None:
            logger.warning(
                "Slurm accepted submission %s but its job identity is indeterminate",
                submission_id,
            )
            return True
        intent = update_intent(context.server, intent, phase="accepted", job_id=match.group(1))
        await context.server.slurm.scheduler_accepted(
            context.server.tasks.execution(entry), submission_id, match.group(1)
        )
        forget_intent(context.server, intent)
        return True
    except Exception as exc:
        error = sanitized_tail(str(exc), None) or type(exc).__name__
        if submitted:
            # Neither a timeout nor a lost API reply proves sbatch did not apply.
            logger.warning(
                "Submission %s requires scheduler reconciliation: %s",
                submission_id,
                error,
            )
            return True
        await _persist_submission_failure(entry, error)
        if submission_id is not None:
            forget_intent(context.server, intent)
        return False
