"""Build the Apptainer command and configure its bind mounts."""

import os
from pathlib import Path

from simstack.runner_runtime.container_environment import (
    CONTAINER_WORKDIR,
    LEGACY_CONTAINER_WORKDIR,
    ContainerEnvironment,
)


def build_apptainer_command(
    environment: ContainerEnvironment, *, image: str, workdir: Path | str,
    resource_args: list[str], task_args: list[str],
) -> list[str]:
    bind_args = [
        "--no-mount", "tmp",
        "--bind", f"{workdir}:{CONTAINER_WORKDIR}",
        "--bind", f"{workdir}:{LEGACY_CONTAINER_WORKDIR}",
    ]
    scratch = os.environ.get("SCRATCH", "").strip()
    if scratch:
        scratch_path = Path(scratch)
        scratch_root = scratch_path.resolve()
        mount_roots = {
            Path(root).resolve() for root in ("/tmp", "/scratch", "/ramdisk")
        }
        if scratch_root not in mount_roots and scratch_path.is_dir():
            bind_args.extend(["--bind", f"{scratch_path}:{scratch_path}"])
    for host_path, dest in environment.mounts:
        bind_args.extend(["--bind", f"{host_path}:{dest}"])
    return [
        "apptainer", "run",
        *resource_args,
        *bind_args,
        image,
        *task_args,
    ]
