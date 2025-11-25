import os
import re
import stat
import subprocess

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry
from simstack.util.project_root_finder import find_project_root
from simstack.util.submit_to_watchdog import submit_to_watchdog
import logging

logger = logging.getLogger("submit_node")

def make_executable(file_path):
    # Get current permissions
    current_permissions = os.stat(file_path).st_mode

    # Add executable bit for user, group and others
    executable_mode = current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

    # Apply new permissions
    os.chmod(file_path, executable_mode)


async def submit_node(registry_entry: NodeRegistry):
    """Submit a node to the SLURM queue"""
    task_id = registry_entry.id
    try:
        logger.info(f"Submitting task_id: {task_id} to SLURM queue")
        # Implement SLURM submission logic here
        base_path = find_project_root()

        python_path = ":".join(context.config.python_path)
        work_dir = os.path.join(context.config.workdir, registry_entry.name, str(registry_entry.id))
        job_name = registry_entry.name + "." + str(registry_entry.id)

        logger.info(f"task_id: {task_id} workdir {work_dir} python path {python_path}")
        # write a slurm script that starts a python script "run_node.py" with the node id as an argument

        slurm_parameters = registry_entry.parameters.slurm_parameters
        if slurm_parameters is None:
            logger.error("Task task_id: {task_id} has no slurm parameters -- failing")
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return
        slurm_parameters.output = f"{work_dir}/%j.out"
        slurm_parameters.error = f"{work_dir}/%j.err"
        slurm_parameters.job_name = f"{job_name}"
        slurm_parameters.chdir = work_dir

        slurm_parameters.startup_commands.append("source ~/.bashrc")
        slurm_parameters.startup_commands.append(f"{context.config.environment_start}")
        slurm_parameters.startup_commands.append(f"export PYTHONPATH={python_path}:$PYTHONPATH")

        if context.config.docker:
            external_work_dir = context.config.external_workdir /  registry_entry.name / str(registry_entry.id)
          
            watcher_file = find_project_root() / "src" / "simstack" / "util" / "queue_watcher.py"
            with open(watcher_file, 'r') as f:
                watcher_content = f.read()
            slurm_parameters.startup_commands.append(f"cat > watcher.py <<'EOF'\n{watcher_content}\nEOF")
            slurm_parameters.startup_commands.append(f"python watcher.py &")
            slurm_parameters.startup_commands.append(f"WATCHER_PID=$!")

            slurm_parameters.startup_commands.append("set -euo pipefail")
            slurm_parameters.startup_commands.append(
                'cleanup() { '
                'if [ -n "${WATCHER_PID:-}" ] && kill -0 "$WATCHER_PID" 2>/dev/null; then '
                '  kill "$WATCHER_PID" 2>/dev/null || true; '
                '  sleep 2; '
                '  kill -9 "$WATCHER_PID" 2>/dev/null || true; '
                'fi; '
                '}'
            )

            slurm_parameters.startup_commands.append("trap cleanup EXIT INT TERM")
            toml_path = find_project_root() / "simstack_docker.toml"
            if not toml_path.exists():
                logger.error(f"Task task_id: {task_id} has no simstack.toml file -- failing")
                registry_entry.status = TaskStatus.FAILED
                await context.db.save(registry_entry)
                return

            docker_start = "udocker run "
            docker_start += "-e GIT_TOKEN=XXX"
            docker_start += f"-e NODE_ID={registry_entry.id} "
            docker_start += f"-e RESOURCE={str(context.config.resource)} "
            docker_start += f"-v {external_work_dir}:/home/appuser/simstack "
            docker_start += f"-v {context.config.external_source_dir}:/app/simstack-model "
            docker_start += f"-v {toml_path}:/app/simstack-model/simstack.toml "
            docker_start += " simstack-runner"
            slurm_parameters.startup_commands.append(docker_start)
        else:
            slurm_parameters.startup_commands.append(
               f"uv run --directory {base_path} run_node --node-id {registry_entry.id}")

        slurm_script = slurm_parameters.to_sbatch_header()

        # write the script to a file in the work_dir
        os.makedirs(work_dir, exist_ok=True)
        logger.info(f"task_id: {task_id} workdir {work_dir} python path {python_path}")
        script_path = os.path.join(work_dir, "slurm_script.sh")
        with open(script_path, "w") as f:
            f.write(slurm_script)

        make_executable(script_path)
        # submit the script to the slurm queue
        if context.config.docker:
            job_id = "slurm_" + str(registry_entry.id)
            queue_dir = context.config.workdir / "queue"
            result = submit_to_watchdog(f"/usr/bin/sbatch {os.path.join(external_work_dir, 'slurm_script.sh')}", job_id, queue_dir)
        else:
            result = subprocess.run(
                f"/usr/bin/sbatch {os.path.join(work_dir, 'slurm_script.sh')}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30  # Add timeout to prevent hanging
            )

        logger.info(f"submitting job task_id: {task_id} returns: {result.returncode} {result.stdout} {result.stderr}")
        if result.returncode == 0:
            # Extract job ID using a regex pattern
            match = re.search(r'Submitted batch job (\d+)', result.stdout)
            if match:
                job_id = match.group(1)
                logger.info(f"task_id: {task_id} job successfully submitted with job_id: {job_id}")
                registry_entry.job_id = job_id
            else:
                logger.warning(
                    f"task_id: {task_id} job submitted but could not extract job_id from output: {result.stdout}")
        else:
            logger.error(
                f"error submitting job for task_id: {task_id} return code: {result.returncode} stdout: {result.stdout}")
            logger.error(f"submitting job for task_id: {task_id} stderr: {result.stderr}")
            registry_entry.status = TaskStatus.FAILED
            await context.db.save(registry_entry)
            return
        registry_entry.status = TaskStatus.SLURM_QUEUED
        await context.db.save(registry_entry)
    except Exception as e:
        logger.exception(f"fatal error in submitting task_id: {task_id} {str(e)}")
