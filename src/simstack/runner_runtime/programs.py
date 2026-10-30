"""Project only explicit native-program policy from the operator's local config.

No config paths, ambient environment values, API tokens or arbitrary resource
settings cross this boundary. Programs without run_command are worker launch
profiles and are not exposed as native invocations.
"""

from __future__ import annotations

from collections.abc import Mapping

from simstack.runner_protocol.dto.programs import (
    NamedProgramPlanV1,
    RunnerProgramPolicyRequestV1,
)

from .environment import validate_worker_environment_allowlist


def named_program_policy(
    adapter, *, claim_id: str, attempt: int
) -> RunnerProgramPolicyRequestV1:
    resource = adapter._resource
    programs = resource.get("program", {})
    if not isinstance(programs, Mapping):
        raise ValueError("Execution program settings must be a table")
    result = {}
    for name, settings in programs.items():
        if not isinstance(settings, Mapping):
            raise ValueError("Named program settings must be a table")
        if "run_command" not in settings:
            continue
        allowed = list(
            validate_worker_environment_allowlist(
                settings.get("worker_environment_allowlist", [])
            )
        )
        plan = NamedProgramPlanV1(
            command=settings["run_command"],
            scripts=settings.get("scripts", []),
            shell=settings.get("shell", resource.get("shell")),
            environment_allowlist=allowed,
            timeout_seconds=settings.get("timeout_seconds", 3600),
        )
        result[name] = plan
    return RunnerProgramPolicyRequestV1(
        claim_id=claim_id, attempt=attempt, programs=result
    )


async def publish_task_program_policy(
    supervisor_api, adapter, *, task_id: str, claim_id: str, attempt: int
) -> None:
    policy = named_program_policy(adapter, claim_id=claim_id, attempt=attempt)
    if policy.programs:
        await supervisor_api.register_program_policy(task_id, policy=policy)
