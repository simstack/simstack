from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simstack.core.definitions import TaskStatus
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


def test_normalize_non_slurm_queue_keeps_submitted_slurm_parameters():
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

    assert parameters.slurm_parameters.nodes == 4
    assert parameters.slurm_parameters.tasks == 8
    assert parameters.slurm_parameters.tasks_per_node == 2
    assert parameters.slurm_parameters.mem == "16G"
    assert parameters.slurm_parameters.time == "04:00:00"
    assert parameters.slurm_parameters.partition == "batch"
    assert parameters.slurm_parameters.job_name == "previous-slurm-job"
    assert parameters.slurm_parameters.output == "/old/%j.out"
    assert parameters.slurm_parameters.error == "/old/%j.err"
    assert parameters.slurm_parameters.startup_commands == ["run previous node"]
    assert parameters.slurm_parameters.chdir == "/old/workdir"


def test_normalize_legacy_docker_queue_keeps_submitted_slurm_parameters():
    parameters = Parameters(
        queue="docker",
        slurm_parameters=SlurmParameters(cpus_per_task=8, mem="32G"),
    )

    normalize_and_validate_effective_parameters(parameters)

    assert parameters.slurm_parameters.cpus_per_task == 8
    assert parameters.slurm_parameters.mem == "32G"
    assert parameters.queue == "default"
    assert parameters.in_docker is True


def test_normalize_cloud_resource_keeps_slurm_allocation():
    parameters = Parameters(
        resource="cloud",
        queue="default",
        slurm_parameters=SlurmParameters(
            cpus_per_task=4,
            mem="8G",
            time="02:00:00",
        ),
    )

    normalize_and_validate_effective_parameters(parameters)

    assert parameters.slurm_parameters.cpus_per_task == 4
    assert parameters.slurm_parameters.mem == "8G"
    assert parameters.slurm_parameters.time == "02:00:00"


def test_normalize_cloud_queue_keeps_slurm_allocation():
    parameters = Parameters(
        queue="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=2, mem="4G"),
    )

    normalize_and_validate_effective_parameters(parameters)

    assert parameters.slurm_parameters.cpus_per_task == 2
    assert parameters.slurm_parameters.mem == "4G"


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
            resource_str="test",
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
    assert resolution.parameters.resource == "test"
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
async def test_resolve_resource_assignment_without_call_path_keeps_slurm(
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
    assert resolution.parameters.slurm_parameters.nodes == 2
    assert resolution.parameters.slurm_parameters.time == "02:00:00"
    assert resolution.parameters.slurm_parameters.output == "/old/%j.out"
    assert resolution.parameters.slurm_parameters.startup_commands == ["run old node"]


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
async def test_node_registry_creation_applies_resource_assignment(odmantic_engine, initialized_context):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)
    context = initialized_context

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

    # Create and register NodeMapping for the probe function
    from simstack.models import NodeModel
    probe_mapping = NodeModel(
        name="resource_assignment_probe_in_tests",
        function_mapping="workflow.resource_assignment_probe_in_tests",
        input_mappings=[],
        default_parameters=Parameters(),
    )
    node_mappings = context.node_mappings
    node_mappings._by_name["resource_assignment_probe_in_tests"] = probe_mapping
    node_mappings._by_mapping["workflow.resource_assignment_probe_in_tests"] = probe_mapping


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
    odmantic_engine, initialized_context
):
    context = initialized_context
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)

    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="direct-probe-slurm",
            regex_pattern="resource_assignment_probe_in_tests",
            resource_str="test",
            queue="slurm-queue",
            slurm_parameters_patch=SlurmParametersPatch(nodes=3, time="03:00:00"),
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
    )

    # Create and register NodeMapping for the probe function
    from simstack.models import NodeModel
    probe_mapping = NodeModel(
        name="resource_assignment_probe_in_tests",
        function_mapping="workflow.resource_assignment_probe_in_tests",
        input_mappings=[],
        default_parameters=Parameters(),
    )
    node_mappings = context.node_mappings
    node_mappings._by_name["resource_assignment_probe_in_tests"] = probe_mapping
    node_mappings._by_mapping["workflow.resource_assignment_probe_in_tests"] = probe_mapping

    registry_entry = await probe_node.make_registry_entry(
        function_hash="probe-function-hash",
        arg_hash="probe-arg-hash",
    )

    registry_entry = await probe_node.make_registry_entry(
        function_hash="direct-probe-function-hash",
        arg_hash="direct-probe-arg-hash",
    )

    assert registry_entry.call_path == ".resource_assignment_probe_in_tests"
    assert registry_entry.assignment_rule_name == "direct-probe-slurm"
    assert registry_entry.parameters.resource == "test"
    assert registry_entry.parameters.queue == "slurm-queue"
    assert registry_entry.parameters.slurm_parameters.nodes == 3
    assert registry_entry.parameters.slurm_parameters.time == "03:00:00"
    assert probe_node.parameters.resource == "test"
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


@pytest.mark.asyncio
async def test_concrete_resource_rule_legacy_null_flags_become_explicit_false(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    rule = ResourceAssignmentRule(
        name="resource-only",
        regex_pattern="workflow.probe",
        resource_str="cluster-a",
    )
    assert rule.in_docker is False
    assert rule.force_rerun is False
    assert rule.recompute_artifacts is False
    await odmantic_engine.save(rule)

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path="workflow.probe",
        base_parameters=Parameters(
            in_docker=True,
            force_rerun=True,
            recompute_artifacts=True,
        ),
    )

    assert resolution.parameters.in_docker is False
    assert resolution.parameters.force_rerun is False
    assert resolution.parameters.recompute_artifacts is False


@pytest.mark.asyncio
async def test_legacy_rule_documents_are_normalized_before_execution(
    odmantic_engine,
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    collection = odmantic_engine.collection(ResourceAssignmentRule)
    await collection.insert_many(
        [
            {
                "name": "legacy-self",
                "regex_pattern": "workflow.self_child",
                "resource_str": "self",
                "queue": "slurm-queue",
                "in_docker": True,
                "force_rerun": True,
                "recompute_artifacts": True,
                "slurm_parameters": {"nodes": 4, "cpus_per_task": 8, "mem": "32G"},
            },
            {
                "name": "legacy-concrete",
                "regex_pattern": "workflow.remote_child",
                "resource_str": "cluster-a",
                "queue": "default",
            },
        ]
    )

    self_resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path="workflow.self_child",
        base_parameters=Parameters(),
    )
    assert self_resolution.parameters.resource == "self"
    assert self_resolution.parameters.queue == "default"
    assert self_resolution.parameters.in_docker is False
    assert self_resolution.parameters.force_rerun is False
    assert self_resolution.parameters.recompute_artifacts is False
    assert self_resolution.parameters.slurm_parameters.nodes == 1
    assert self_resolution.parameters.slurm_parameters.cpus_per_task == 1

    concrete_resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path="workflow.remote_child",
        base_parameters=Parameters(
            in_docker=True,
            force_rerun=True,
            recompute_artifacts=True,
        ),
    )
    assert concrete_resolution.parameters.in_docker is False
    assert concrete_resolution.parameters.force_rerun is False
    assert concrete_resolution.parameters.recompute_artifacts is False


def test_self_rule_clears_routing_and_execution_overrides():
    rule = ResourceAssignmentRule(
        name="self-child",
        regex_pattern="workflow.child",
        resource_str="self",
        queue="slurm-queue",
        in_docker=True,
        force_rerun=True,
        recompute_artifacts=True,
        slurm_parameters={"nodes": 4, "cpus_per_task": 8, "mem": "32G"},
    )

    assert rule.resource_str == "self"
    assert rule.queue is None
    assert rule.in_docker is None
    assert rule.force_rerun is None
    assert rule.recompute_artifacts is None
    assert rule.slurm_parameters == {}


@pytest.mark.asyncio
async def test_force_rerun_rule_is_resolved_before_cache_lookup(
    odmantic_engine, initialized_context, monkeypatch
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="force-probe",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            force_rerun=True,
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )
    find_reusable_task = AsyncMock()
    monkeypatch.setattr(
        "simstack.core.node._find_reusable_task", find_reusable_task
    )

    async def fake_make_registry_entry(function_hash, arg_hash):
        probe_node.registry_entry = SimpleNamespace(status=TaskStatus.SUBMITTED)
        return probe_node.registry_entry

    monkeypatch.setattr(probe_node, "make_registry_entry", fake_make_registry_entry)

    status = await probe_node.get_node_registry()

    assert probe_node.parameters.force_rerun is True
    find_reusable_task.assert_not_awaited()
    assert status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_recompute_rule_is_resolved_before_cached_result_handling(
    odmantic_engine, initialized_context, monkeypatch
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="recompute-probe",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            recompute_artifacts=True,
        )
    )

    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )
    cached = SimpleNamespace(
        id="cached-task",
        parent_ids=[],
        parameters=Parameters(resource="cluster-a"),
        status=TaskStatus.COMPLETED,
    )
    find_reusable_task = AsyncMock(return_value=cached)
    monkeypatch.setattr(
        "simstack.core.node._find_reusable_task", find_reusable_task
    )
    recompute = AsyncMock()
    monkeypatch.setattr(
        "simstack.core.recompute_artifacts.recompute_artifacts", recompute
    )

    status = await probe_node.get_node_registry()

    assert probe_node.recompute_artifacts is True
    find_reusable_task.assert_awaited_once()
    recompute.assert_awaited_once_with(cached)
    assert status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_same_route_cached_task_keeps_its_persisted_routing(
    odmantic_engine, initialized_context, monkeypatch
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="persisted-route",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            queue="default",
            in_docker=True,
            slurm_parameters={"nodes": 2, "mem": "8G"},
        )
    )

    persisted_parameters = Parameters(
        resource="cluster-a",
        queue="default",
        in_docker=True,
        slurm_parameters=SlurmParameters(nodes=2, mem="8G"),
    )
    cached = SimpleNamespace(
        id="cached-task",
        parent_ids=[],
        parameters=persisted_parameters,
        status=TaskStatus.SUBMITTED,
    )
    find_reusable_task = AsyncMock(return_value=cached)
    monkeypatch.setattr(
        "simstack.core.node._find_reusable_task", find_reusable_task
    )
    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )

    status = await probe_node.get_node_registry()

    assert status == TaskStatus.SUBMITTED
    find_reusable_task.assert_awaited_once()
    assert probe_node.parameters is persisted_parameters
    assert probe_node.parameters.resource == "cluster-a"
    assert probe_node.parameters.queue == "default"
    assert probe_node.parameters.in_docker is True
    assert probe_node.parameters.slurm_parameters.nodes == 2


@pytest.mark.asyncio
async def test_new_registry_uses_one_assignment_resolution_snapshot(
    odmantic_engine, initialized_context, monkeypatch
):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await _ensure_probe_node_model(odmantic_engine)
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="single-snapshot",
            regex_pattern="workflow.resource_assignment_probe_in_tests",
            resource_str="cluster-a",
            force_rerun=True,
        )
    )

    probe_mapping = NodeModel(
        name="resource_assignment_probe_in_tests",
        function_mapping="workflow.resource_assignment_probe_in_tests",
        input_mappings=[],
        default_parameters=Parameters(),
    )
    initialized_context.node_mappings._by_name[
        "resource_assignment_probe_in_tests"
    ] = probe_mapping
    initialized_context.node_mappings._by_mapping[
        "workflow.resource_assignment_probe_in_tests"
    ] = probe_mapping

    resolution_calls = 0
    original_resolve = resolve_resource_assignment

    async def counting_resolve(*args, **kwargs):
        nonlocal resolution_calls
        resolution_calls += 1
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        "simstack.core.node.resolve_resource_assignment", counting_resolve
    )
    monkeypatch.setattr(
        "simstack.core.resource_assignment.resolve_resource_assignment",
        counting_resolve,
    )
    probe_node = Node(
        func=resource_assignment_probe_in_tests,
        is_async=False,
        parameters=Parameters(),
        call_path=".workflow.resource_assignment_probe_in_tests",
    )

    status = await probe_node.get_node_registry()

    assert resolution_calls == 1
    assert status == TaskStatus.SUBMITTED
    assert probe_node.registry_entry is not None
    assert probe_node.registry_entry.assignment_rule_name == "single-snapshot"
    assert probe_node.registry_entry.parameters.resource == "cluster-a"
    assert probe_node.registry_entry.parameters.force_rerun is True
    assert probe_node._pending_resource_assignment_resolution is None


@pytest.mark.asyncio
async def test_explicit_false_rule_flags_are_effective_overrides(odmantic_engine):
    await _delete_all(odmantic_engine, ResourceAssignmentRule)
    await odmantic_engine.save(
        ResourceAssignmentRule(
            name="disable-runtime-flags",
            regex_pattern="workflow.probe",
            in_docker=False,
            force_rerun=False,
            recompute_artifacts=False,
        )
    )

    resolution = await resolve_resource_assignment(
        odmantic_engine,
        call_path="workflow.probe",
        base_parameters=Parameters(
            in_docker=True,
            force_rerun=True,
            recompute_artifacts=True,
        ),
    )

    assert resolution.parameters.in_docker is False
    assert resolution.parameters.force_rerun is False
    assert resolution.parameters.recompute_artifacts is False
