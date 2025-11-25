import asyncio
import logging
import os
import re
import socket
import subprocess
from pathlib import Path

from simstack.core.context import context
from simstack.models.runner_model import RunnerEvent, RunnerType, RunnerEventEnum

logger = logging.getLogger("RunnerUtil")


def ensure_crontab_entry(command, schedule="*/10 * * * *"):
    """
    Checks if a crontab entry exists and adds it if it doesn't.

    Args:
        command (str): The command to be executed by cron
        schedule (str): The cron schedule expression (default: every 10 minutes)

    Returns:
        bool: True if entry was added, False if it already existed
    """
    # The full crontab entry to look for
    cron_entry = f"{schedule} {command}"

    try:
        # Get current crontab
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)

        # Check if command succeeded
        if result.returncode == 0:
            current_crontab = result.stdout
        else:
            # If crontab is empty or doesn't exist
            current_crontab = ""

        # Check if our entry already exists (using regex to handle whitespace variations)
        entry_pattern = (
            re.escape(schedule.strip()) + r"\s+" + re.escape(command.strip())
        )
        if re.search(entry_pattern, current_crontab):
            logger.info(f"Crontab entry for '{command}' already exists.")

            return False

        # Add our entry to crontab
        new_crontab = current_crontab
        if new_crontab and not new_crontab.endswith("\n"):
            new_crontab += "\n"
        new_crontab += cron_entry + "\n"

        # Write back to crontab
        process = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
        process.communicate(input=new_crontab)

        if process.returncode == 0:
            logger.info(f"Successfully added crontab entry: '{cron_entry}'")
            return True
        else:
            logger.error(f"Failed to update crontab, return code: {process.returncode}")
            return False

    except Exception as e:
        logger.error(f"Error managing crontab: {str(e)}")
        return False


async def graceful_shutdown(resource_name):
    # Log shutdown

    pid = os.getpid()
    user = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    hostname = socket.gethostname()

    # Capture current user info for the subprocess
    current_user = os.environ.get("USER", os.environ.get("USERNAME"))

    runner_event = RunnerEvent(
        resource=resource_name,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.SHUTDOWN,
        pid=pid,
        user=user,
        hostname=hostname,
        message=f"Graceful shutdown of {pid} by {current_user}",
    )
    await context.db.save(runner_event)

    # Cancel all remaining tasks
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()

    # Give tasks time to cancel
    await asyncio.sleep(0.5)

    # Signal to the main function to exit
    raise SystemExit(0)


async def restart(resource_name):
    logger.info(
        f"Restarting node runner for resource: {resource_name} {context.config.python_path}"
    )

    cmd = Path(context.config.python_path[0]) / "scripts" / "check_runner.sh"

    # Get current user information
    current_uid = os.getuid()
    current_gid = os.getgid()
    current_pid = os.getpid()
    logger.info(f"Current user ID: {current_uid}, group ID: {current_gid}")

    # Create environment with preserved user information
    env = os.environ.copy()

    # Start a new process with the same arguments

    logger.info(f"Restarting node runner for resource: {resource_name} {str(cmd)}")

    result = subprocess.Popen(str(cmd), start_new_session=True, env=env)
    # Log the restart
    message = f"user: {current_uid} group: {current_gid} pid: {current_pid} result: {result.pid}"
    runner_event = RunnerEvent(
        resource=resource_name,
        runner_type=RunnerType.RESOURCE_RUNNER,
        event=RunnerEventEnum.RESTART,
        message=message,
    )
    await context.db.save(runner_event)
    # Exit current process
    logger.info(f"Restarted node runner for resource: {resource_name}")
    # await graceful_shutdown(resource_name)


async def schedule_restart(resource_name, restart_minutes):
    """Schedule a restart after the specified minutes"""
    try:
        logger.info(
            f"Runner will restart in {restart_minutes} minutes on resource {resource_name} "
        )
        await asyncio.sleep(restart_minutes * 60)  # Convert minutes to seconds
        # try:
        #     if context.config.python_path:
        #         path = Path(context.config.python_path[0])
        #         command = str(path / "scripts" / "check_runner.sh") + " >> " + str(path / "cron_log.log") + " 2>&1"
        #         logger.info(f"Path: {command} ")
        #         if not ensure_crontab_entry(command):
        #             runner_entry = RunnerEvent(resource=resource_name, runner_type=RunnerType.RESOURCE_RUNNER,
        #                                        event=RunnerEventEnum.CRONTAB_GONE,
        #                                        message="Crontab entry for resource runner was not found")
        #             await context.db.save(runner_entry)
        # except Exception as e:
        #     logger.error("Something went wrong for crontab")
        await restart(resource_name)
        await graceful_shutdown(resource_name)
    except asyncio.CancelledError:
        logger.info("Restart task was cancelled")
        raise
