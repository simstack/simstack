"""Recovery identifies its original task without replacing its live coordinator."""
from types import SimpleNamespace

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node import Node
from simstack.models import NodeRegistry, Parameters
from simstack.runner_runtime.launch_claims import task_execution


@pytest.mark.asyncio
async def test_recovery_invocation_sends_original_generation_and_current_coordinator(monkeypatch):
    def probe(**kwargs):
        return None

    original = NodeRegistry(name="probe", status=TaskStatus.FAILED,
        function_hash="", arg_hash="", func_mapping="test.probe", parameters=Parameters())
    coordinator = NodeRegistry(name="coordinator", status=TaskStatus.RUNNING,
        function_hash="", arg_hash="", func_mapping="test.coordinator", parameters=Parameters())
    object.__setattr__(original, "_execution_generation", "original-generation")
    object.__setattr__(coordinator, "_execution_generation", "coordinator-generation")
    object.__setattr__(coordinator, "_execution_claim_id", "coordinator-claim")
    object.__setattr__(coordinator, "_execution_start_id", "coordinator-start")
    outgoing = []

    class ResolutionBoundary(Exception):
        pass

    async def resolve(payload, *, request_id):
        outgoing.append(payload)
        raise ResolutionBoundary

    active = SimpleNamespace(
        server=SimpleNamespace(tasks=SimpleNamespace(resolve_invocation=resolve)),
        node_mappings=SimpleNamespace(get_by_name=lambda _: SimpleNamespace(function_mapping="test.probe")),
    )
    monkeypatch.setattr("simstack.core.node.context", active)
    invocation = Node(func=probe, is_async=False, parent_id=coordinator.id,
        parameters=Parameters(), recovery_of=original)

    with task_execution(coordinator), pytest.raises(ResolutionBoundary):
        await invocation.get_node_registry()

    assert outgoing[0]["recovery_of"] == {
        "task_id": str(original.id), "generation": "original-generation",
    }
    assert outgoing[0]["parent_execution"] == {
        "task_id": str(coordinator.id), "generation": "coordinator-generation",
        "claim_id": "coordinator-claim", "start_id": "coordinator-start",
    }
    assert "recovery_of" not in invocation._function_kwargs
