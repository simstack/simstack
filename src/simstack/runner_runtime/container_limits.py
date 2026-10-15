"""Pure resource-limit projection shared by Docker and Slurm launch adapters."""

import re

_SLURM_MEMORY_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([MGmg])B?$")

def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _slurm_task_count(slurm: object) -> int:
    tasks = _positive_int(getattr(slurm, "tasks", None))
    if tasks is not None:
        return tasks
    tasks_per_node = _positive_int(getattr(slurm, "tasks_per_node", None))
    if tasks_per_node is not None:
        return tasks_per_node
    return 1


def docker_cpu_limit(slurm: object | None) -> int | None:
    """Docker/Apptainer CPU count: cpus_per_task * tasks (or tasks_per_node)."""
    if slurm is None:
        return None

    cpus_per_task = _positive_int(getattr(slurm, "cpus_per_task", None))
    tasks = _positive_int(getattr(slurm, "tasks", None))
    tasks_per_node = _positive_int(getattr(slurm, "tasks_per_node", None))
    if cpus_per_task is None and tasks is None and tasks_per_node is None:
        return None

    return (cpus_per_task or 1) * _slurm_task_count(slurm)


def _parse_slurm_memory(value: object) -> tuple[float, str] | None:
    if not isinstance(value, str):
        return None
    match = _SLURM_MEMORY_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return float(match.group(1)), match.group(2).lower()


def _format_container_memory(amount: float, unit: str, *, uppercase: bool) -> str:
    formatted_amount = str(int(amount)) if amount == int(amount) else str(amount)
    formatted_unit = unit.upper() if uppercase else unit.lower()
    return f"{formatted_amount}{formatted_unit}"


def docker_memory_limit(slurm: object | None, *, uppercase: bool = False) -> str | None:
    """Container memory from Slurm ``mem``, or ``mem_per_cpu`` times CPU count."""
    if slurm is None:
        return None

    mem = _parse_slurm_memory(getattr(slurm, "mem", None))
    if mem is not None:
        amount, unit = mem
        return _format_container_memory(amount, unit, uppercase=uppercase)

    mem_per_cpu = _parse_slurm_memory(getattr(slurm, "mem_per_cpu", None))
    if mem_per_cpu is None:
        return None

    cpu_count = docker_cpu_limit(slurm) or 1
    amount, unit = mem_per_cpu
    return _format_container_memory(amount * cpu_count, unit, uppercase=uppercase)


def container_resource_args(docker_cmd: str, slurm: object | None) -> list[str]:
    """Runtime flags that pin CPU and memory for docker or apptainer."""
    args: list[str] = []
    cpu_limit = docker_cpu_limit(slurm)
    if cpu_limit is not None:
        args.extend(["--cpus", str(cpu_limit)])

    memory_limit = docker_memory_limit(slurm, uppercase=docker_cmd == "apptainer")
    if memory_limit is not None:
        args.extend(["--memory", memory_limit])
    return args
