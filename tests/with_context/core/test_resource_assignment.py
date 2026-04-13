from types import SimpleNamespace

import pytest

from simstack.core.resource_assignment import (
    apply_resource_assignment_to_node_registry,
    resolve_resource_assignment,
)
from simstack.core.resources import allowed_resources
from simstack.models import ResourceAssignmentRule, SlurmParametersPatch
from simstack.models.parameters import Parameters, SlurmParameters


async def _delete_all(engine, model):
    existing = await engine.find(model)
    for item in existing:
        await engine.delete(item)


@pytest.fixture(autouse=True)
def _allow_assignment_resources():
    original_resources = allowed_resources.get_resources()
    allowed_resources.add_resource("cluster-a")
    allowed_resources.add_resource("cluster-b")
    yield
    allowed_resources._resources = original_resources


@pytest.mark.asyncio
async def test_resolve_resource_assignment_matches_path_and_normalizes_slurm(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="master-orca",
            regex_pattern="master.*.orca",
            priority=10,
            resource_str="cluster-a",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=4, time="04:00:00"),
        )
    )

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path=".master.step.orca",
        base_parameters=Parameters(),
    )

    assert resolution.normalized_call_path == "master.step.orca"
    assert resolution.matched_rule is not None
    assert resolution.matched_rule.name == "master-orca"
    assert resolution.parameters.resource == "cluster-a"
    assert resolution.parameters.queue == "slurm-queue"
    assert resolution.parameters.slurm_parameters.nodes == 4
    assert resolution.parameters.slurm_parameters.time == "04:00:00"
    assert resolution.parameters.slurm_parameters.tasks is None
    assert resolution.parameters.slurm_parameters.tasks_per_node is None


@pytest.mark.asyncio
async def test_resolve_resource_assignment_uses_most_specific_path_pattern(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="generic-gaussian",
            regex_pattern="*.gaussian",
            resource_str="cluster-a",
        )
    )
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="master-gaussian",
            regex_pattern="master.*.gaussian",
            resource_str="cluster-b",
        )
    )

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path=".master.step.gaussian",
        base_parameters=Parameters(),
    )

    assert resolution.matched_rule is not None
    assert resolution.matched_rule.name == "master-gaussian"
    assert resolution.parameters.resource == "cluster-b"


@pytest.mark.asyncio
async def test_resolve_resource_assignment_rejects_equally_specific_matches(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="rule-a",
            regex_pattern="maste*.step.gaussian",
            resource_str="cluster-a",
        )
    )
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="rule-b",
            regex_pattern="master.ste*.gaussian",
            resource_str="cluster-b",
        )
    )

    with pytest.raises(ValueError, match="Ambiguous resource assignment"):
        await resolve_resource_assignment(
            odmantic_engine,
            call_path=".master.step.gaussian",
            base_parameters=Parameters(),
        )


@pytest.mark.asyncio
async def test_resolve_resource_assignment_rejects_nested_slurm(odmantic_engine):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="master-orca",
            regex_pattern="master.*.orca",
            priority=5,
            resource_str="cluster-a",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=2),
        )
    )

    with pytest.raises(ValueError, match="Nested Slurm allocation is not allowed"):
        await resolve_resource_assignment(
            odmantic_engine,
            call_path=".master.step.orca",
            base_parameters=Parameters(),
            parent_parameters=Parameters(
                queue="slurm-queue",
                slurm_parameters=SlurmParameters(nodes=1),
            ),
        )


@pytest.mark.asyncio
async def test_apply_resource_assignment_sets_trace_fields(odmantic_engine):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    saved_rule = await odmantic_engine.save(
        ResourceAssignmentRule(
            name="master-orca",
            regex_pattern="master.*.orca",
            priority=10,
            resource_str="cluster-a",
        )
    )

    node_registry = SimpleNamespace(
        call_path=".master.step.orca",
        parameters=Parameters(),
        assignment_rule_id=None,
        assignment_rule_name=None,
        assignment_pattern=None,
    )

    resolution = await apply_resource_assignment_to_node_registry(
        odmantic_engine, node_registry
    )

    assert resolution.matched_rule is not None
    assert node_registry.assignment_rule_id == str(saved_rule.id)
    assert node_registry.assignment_rule_name == "master-orca"
    assert node_registry.assignment_pattern == "master.*.orca"
    assert node_registry.parameters.resource == "cluster-a"
