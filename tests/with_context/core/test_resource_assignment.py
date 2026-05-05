from types import SimpleNamespace

import pytest

from simstack.core.node import Node
from simstack.core.resource_assignment import (
    apply_resource_assignment_to_node_registry,
    normalize_and_validate_effective_parameters,
    resolve_resource_assignment,
)
from simstack.core.resources import allowed_resources
from simstack.models import NodeModel, ResourceAssignmentRule, SlurmParametersPatch
from simstack.models.parameters import Parameters, SlurmParameters


def resource_assignment_probe_in_tests(**kwargs):
    return None


def test_normalize_slurm_allocation_accepts_empty_slurm_parameters_defaults():
    parameters = Parameters(
        queue="slurm-queue",
        slurm_parameters=SlurmParameters(),
    )

    normalize_and_validate_effective_parameters(parameters)

    assert parameters.slurm_parameters.nodes == 1
    assert parameters.slurm_parameters.tasks is None
    assert parameters.slurm_parameters.tasks_per_node is None


def test_normalize_slurm_allocation_rejects_tasks_per_node_without_nodes_or_tasks():
    parameters = Parameters(
        queue="slurm-queue",
        slurm_parameters=SlurmParameters(tasks_per_node=4),
    )

    with pytest.raises(ValueError, match='one of "nodes" or "tasks"'):
        normalize_and_validate_effective_parameters(parameters)


def test_normalize_non_slurm_queue_clears_stale_slurm_submission_fields():
    parameters = Parameters(
        queue="default",
        slurm_parameters=SlurmParameters(
            nodes=4,
            tasks=8,
            tasks_per_node=2,
            mem="16G",
            time="04:00:00",
            partition="batch",
            job_name="previous-slurm-job",
            output="/old/%j.out",
            error="/old/%j.err",
            startup_commands=["run previous node"],
            chdir="/old/workdir",
        ),
    )

    normalize_and_validate_effective_parameters(parameters)

    assert parameters.slurm_parameters.nodes is None
    assert parameters.slurm_parameters.tasks is None
    assert parameters.slurm_parameters.tasks_per_node is None
    assert parameters.slurm_parameters.mem is None
    assert parameters.slurm_parameters.time is None
    assert parameters.slurm_parameters.partition is None
    assert parameters.slurm_parameters.job_name is None
    assert parameters.slurm_parameters.output is None
    assert parameters.slurm_parameters.error is None
    assert parameters.slurm_parameters.startup_commands == []
    assert parameters.slurm_parameters.chdir is None


async def _delete_all(engine, model):
    existing = await engine.find(model)
    for item in existing:
        await engine.delete(item)


async def _ensure_probe_node_model(engine):
    existing = await engine.find_one(
        NodeModel, NodeModel.name == "resource_assignment_probe_in_tests"
    )
    if existing is not None:
        return existing

    return await engine.save(
        NodeModel(
            name="resource_assignment_probe_in_tests",
            function_mapping=(
                "tests.with_context.core.test_resource_assignment:"
                "resource_assignment_probe_in_tests"
            ),
            input_mappings=[],
            result_mappings=[],
            default_parameters=Parameters(),
        )
    )


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
async def test_resolve_resource_assignment_allows_nested_slurm(odmantic_engine):
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

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path=".master.step.orca",
        base_parameters=Parameters(),
        parent_parameters=Parameters(
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )

    assert resolution.parameters.queue == "slurm-queue"
    assert resolution.parameters.slurm_parameters.nodes == 2


@pytest.mark.asyncio
async def test_resolve_resource_assignment_allows_nested_slurm_from_base_parameters(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path=(
            ".many_orca_master_job_mariana_cluster_and_gather_jinja."
            "many_orca_jobs_from_single_smiles_cluster_and_gather_jinja"
        ),
        base_parameters=Parameters(
            resource="cluster-a",
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(),
        ),
        parent_parameters=Parameters(
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
    )

    assert resolution.normalized_call_path == (
        "many_orca_master_job_mariana_cluster_and_gather_jinja."
        "many_orca_jobs_from_single_smiles_cluster_and_gather_jinja"
    )
    assert resolution.matched_rule is None
    assert resolution.parameters.resource == "cluster-a"
    assert resolution.parameters.queue == "slurm-queue"
    assert resolution.parameters.slurm_parameters.nodes == 1
    assert resolution.parameters.slurm_parameters.tasks is None
    assert resolution.parameters.slurm_parameters.tasks_per_node is None


@pytest.mark.asyncio
async def test_resolve_resource_assignment_without_call_path_clears_stale_slurm(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path="",
        base_parameters=Parameters(
            queue="default",
            slurm_parameters=SlurmParameters(
                nodes=2,
                time="02:00:00",
                output="/old/%j.out",
                startup_commands=["run old node"],
            ),
        ),
    )

    assert resolution.normalized_call_path == ""
    assert resolution.matched_rule is None
    assert resolution.parameters.queue == "default"
    assert resolution.parameters.slurm_parameters.nodes is None
    assert resolution.parameters.slurm_parameters.time is None
    assert resolution.parameters.slurm_parameters.output is None
    assert resolution.parameters.slurm_parameters.startup_commands == []


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


@pytest.mark.asyncio
async def test_node_registry_creation_applies_resource_assignment(odmantic_engine):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="probe-slurm",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=2, time="02:00:00"),
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )

    registry_entry = await probe_node.make_registry_entry(
        function_hash="probe-function-hash",
        arg_hash="probe-arg-hash",
    )

    assert registry_entry.parameters.resource == "cluster-a"
    assert registry_entry.parameters.queue == "slurm-queue"
    assert registry_entry.parameters.slurm_parameters.nodes == 2
    assert registry_entry.parameters.slurm_parameters.time == "02:00:00"
    assert registry_entry.parameters.slurm_parameters.tasks is None
    assert registry_entry.assignment_rule_name == "probe-slurm"
    assert (
        registry_entry.assignment_pattern
        == "workflow.resource_assignment_probe_in_tests"
    )


@pytest.mark.asyncio
async def test_direct_node_creation_uses_default_call_path_and_syncs_assignment(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="direct-probe-slurm",
            regex_pattern="resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=3, time="03:00:00"),
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
    )

    registry_entry = await probe_node.make_registry_entry(
        function_hash="direct-probe-function-hash",
        arg_hash="direct-probe-arg-hash",
    )

    assert registry_entry.call_path == ".resource_assignment_probe_in_tests"
    assert registry_entry.assignment_rule_name == "direct-probe-slurm"
    assert registry_entry.parameters.resource == "cluster-a"
    assert registry_entry.parameters.queue == "slurm-queue"
    assert registry_entry.parameters.slurm_parameters.nodes == 3
    assert registry_entry.parameters.slurm_parameters.time == "03:00:00"
    assert probe_node.parameters.resource == "cluster-a"
    assert probe_node.parameters.queue == "slurm-queue"
    assert probe_node.parameters.slurm_parameters.nodes == 3


@pytest.mark.asyncio
async def test_node_registry_creation_allows_nested_slurm_assignment(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="probe-slurm",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=2),
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        parent_parameters=Parameters(
            queue="slurm-queue",
            slurm_parameters=SlurmParameters(nodes=1),
        ),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )

    registry_entry = await probe_node.make_registry_entry(
        function_hash="nested-probe-function-hash",
        arg_hash="nested-probe-arg-hash",
    )

    assert registry_entry.parameters.queue == "slurm-queue"
    assert registry_entry.parameters.slurm_parameters.nodes == 2
    assert registry_entry.assignment_rule_name == "probe-slurm"
