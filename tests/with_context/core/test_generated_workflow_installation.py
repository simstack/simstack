from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.generated_workflow import (
    canonical_generated_module_name,
    canonical_source_sha256,
    generated_module_path,
)
from simstack.core.node import node_from_database
from simstack.methods.generated_workflow import install_generated_workflow
from simstack.models.generated_workflow import (
    GeneratedWorkflowSource,
    GeneratedWorkflowStatus,
)
from simstack.models.models import ModelMapping, NodeModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.models.resource_definition import ResourceDefinition


SOURCE_TEMPLATE = """from odmantic import Model

from simstack.core.node import node
from simstack.models.simstack_model import simstack_model

@simstack_model
class GeneratedNumber(Model):
    value: int

@node(expose_in_submit=False)
async def generated_child(**kwargs) -> GeneratedNumber:
    return GeneratedNumber(value={value})

@node(expose_in_submit=True)
async def generated_entrypoint(**kwargs) -> GeneratedNumber:
    return await generated_child(**kwargs)
"""


@pytest_asyncio.fixture(autouse=True)
async def _isolate_generated_registration_tables(initialized_context):
    for model_type in (NodeModel, ModelMapping):
        registrations = await context.db.find(model_type)
        for registration in registrations:
            if registration.source_revision is not None:
                await context.db.delete(registration)
    yield


def _source(*, revision: int, value: int, target_resource: str = "test"):
    source_code = SOURCE_TEMPLATE.format(value=value)
    source_sha256 = canonical_source_sha256(source_code)
    return GeneratedWorkflowSource(
        workflow_id="db-pinning",
        revision=revision,
        title="DB pinning",
        namespace="simstack_generated",
        module_name=canonical_generated_module_name(
            "db-pinning",
            revision,
            source_sha256,
        ),
        entrypoint_name="generated_entrypoint",
        source_code=source_code,
        source_sha256=source_sha256,
        target_resource=target_resource,
    )


@pytest.mark.asyncio
async def test_installer_registers_exact_source_and_marks_ready(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    resource_definition = await context.db.find_one(
        ResourceDefinition,
        ResourceDefinition.resource_str == "test",
    )
    assert resource_definition is not None
    original_queue = resource_definition.queue
    resource_definition.queue = "docker"
    await context.db.save(resource_definition)

    try:
        source = await context.db.save(_source(revision=1, value=1))

        result = await install_generated_workflow._inner(source)

        assert result is True
        stored_source = await context.db.find_one(
            GeneratedWorkflowSource,
            GeneratedWorkflowSource.id == source.id,
        )
        assert stored_source.status == GeneratedWorkflowStatus.READY
        module_path = generated_module_path(source)
        node_models = await context.db.find(
            NodeModel,
            (NodeModel.source_revision == source.id)
            & (NodeModel.source_sha256 == source.source_sha256),
        )
        node_models_by_name = {
            node_model.name: node_model for node_model in node_models
        }
        model_mapping = await context.db.find_one(
            ModelMapping,
            ModelMapping.mapping == f"{module_path}.GeneratedNumber",
        )
        assert model_mapping is not None
        for node_name in ("generated_entrypoint", "generated_child"):
            node_model = node_models_by_name[node_name]
            assert node_model.default_parameters.resource == source.target_resource
            assert node_model.default_parameters.queue == "docker"
            cached_node_model = context.node_mappings.get_by_mapping(
                f"{module_path}.{node_name}"
            )
            assert cached_node_model is not None
            assert (
                cached_node_model.default_parameters.resource == source.target_resource
            )
            assert cached_node_model.default_parameters.queue == "docker"
        entrypoint_model = node_models_by_name["generated_entrypoint"]
        assert entrypoint_model.source_revision == source.id
        assert entrypoint_model.source_sha256 == source.source_sha256
        assert model_mapping.source_revision == source.id
        assert model_mapping.source_sha256 == source.source_sha256
        assert (tmp_path / "simstack_generated" / f"{source.module_name}.py").is_file()
    finally:
        resource_definition.queue = original_queue
        await context.db.save(resource_definition)


@pytest.mark.asyncio
async def test_installer_rejects_cross_workflow_registration_name_collisions(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    first = _source(revision=1, value=17)
    first.workflow_id = f"collision-owner-{tmp_path.name}"
    first.module_name = canonical_generated_module_name(
        first.workflow_id,
        first.revision,
        first.source_sha256,
    )
    second = _source(revision=1, value=18)
    second.workflow_id = f"collision-contender-{tmp_path.name}"
    second.module_name = canonical_generated_module_name(
        second.workflow_id,
        second.revision,
        second.source_sha256,
    )
    first = await context.db.save(first)
    second = await context.db.save(second)

    assert await install_generated_workflow._inner(first) is True
    first_entrypoint_mapping = f"{generated_module_path(first)}.generated_entrypoint"
    first_model_mapping = f"{generated_module_path(first)}.GeneratedNumber"

    assert await install_generated_workflow._inner(second) is False

    stored_second = await context.db.find_one(
        GeneratedWorkflowSource,
        GeneratedWorkflowSource.id == second.id,
    )
    assert stored_second is not None
    assert stored_second.status == GeneratedWorkflowStatus.FAILED
    assert "already registered by another workflow" in stored_second.error
    entrypoint = await context.db.find_one(
        NodeModel,
        NodeModel.name == "generated_entrypoint",
    )
    model = await context.db.find_one(
        ModelMapping,
        ModelMapping.name == "GeneratedNumber",
    )
    assert entrypoint is not None
    assert entrypoint.function_mapping == first_entrypoint_mapping
    assert entrypoint.source_revision == first.id
    assert model is not None
    assert model.mapping == first_model_mapping
    assert model.source_revision == first.id


@pytest.mark.asyncio
async def test_installer_marks_source_failed_for_unregistered_node_input_mapping(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    source_code = """from simstack.core.node import node
from simstack.models import GeneratedWorkflowStatus

@node(expose_in_submit=True)
def generated_entrypoint(
    status: GeneratedWorkflowStatus,
    **kwargs,
) -> GeneratedWorkflowStatus:
    return status
"""
    source_sha256 = canonical_source_sha256(source_code)
    workflow_id = f"missing-input-mapping-{tmp_path.name}"
    source = GeneratedWorkflowSource(
        workflow_id=workflow_id,
        revision=1,
        title="Missing input mapping",
        namespace="simstack_generated",
        module_name=canonical_generated_module_name(
            workflow_id,
            1,
            source_sha256,
        ),
        entrypoint_name="generated_entrypoint",
        source_code=source_code,
        source_sha256=source_sha256,
        target_resource="test",
    )
    source = await context.db.save(source)

    result = await install_generated_workflow._inner(source)

    assert result is False
    stored_source = await context.db.find_one(
        GeneratedWorkflowSource,
        GeneratedWorkflowSource.id == source.id,
    )
    assert stored_source.status == GeneratedWorkflowStatus.FAILED
    assert "generated_entrypoint.status" in stored_source.error
    assert (
        "simstack.models.generated_workflow.GeneratedWorkflowStatus"
        in stored_source.error
    )
    assert "unregistered ModelMapping" in stored_source.error


@pytest.mark.asyncio
async def test_old_registry_imports_r1_after_r2_registration(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    workflow_id = f"db-pinning-{tmp_path.name}"
    revision_one = _source(revision=1, value=1)
    revision_one.workflow_id = workflow_id
    revision_one.module_name = canonical_generated_module_name(
        workflow_id, 1, revision_one.source_sha256
    )
    revision_two = _source(revision=2, value=2)
    revision_two.workflow_id = workflow_id
    revision_two.module_name = canonical_generated_module_name(
        workflow_id, 2, revision_two.source_sha256
    )
    revision_one = await context.db.save(revision_one)
    revision_two = await context.db.save(revision_two)
    assert await install_generated_workflow._inner(revision_one)
    assert await install_generated_workflow._inner(revision_two)

    registry = NodeRegistry(
        name="generated_entrypoint",
        status=TaskStatus.SUBMITTED,
        # The generated source is already SHA-pinned, which used to skip the
        # only branch that detected async functions.
        function_hash=revision_one.source_sha256,
        arg_hash="NOT INITIALIZED",
        func_mapping=(f"{generated_module_path(revision_one)}.generated_entrypoint"),
        source_revision=revision_one.id,
        source_sha256=revision_one.source_sha256,
        parameters=Parameters(),
        # Reproduce a stale Submit/registry flag for an immutable generated
        # source whose actual entrypoint is async.
        is_async=False,
    )
    await context.db.save(registry)

    node = await node_from_database(registry)

    assert node is not None
    assert node._func.__module__ == generated_module_path(revision_one)
    assert registry.function_hash == revision_one.source_sha256
    assert registry.source_revision == revision_one.id
    assert registry.source_sha256 == revision_one.source_sha256
    assert node.is_async is True
    assert registry.is_async is True
    stored_registry = await context.db.find_one(
        NodeRegistry, NodeRegistry.id == registry.id
    )
    assert stored_registry is not None
    assert stored_registry.is_async is True

    result = await node.execute_node_locally()
    assert result.value == 1
    child = await context.db.find_one(
        NodeRegistry,
        (NodeRegistry.name == "generated_child")
        & (NodeRegistry.parent_ids.in_([registry.id])),
    )
    assert child is not None
    assert (
        child.func_mapping == f"{generated_module_path(revision_one)}.generated_child"
    )
    assert child.function_hash == revision_one.source_sha256
    assert child.source_revision == revision_one.id
    assert child.source_sha256 == revision_one.source_sha256


@pytest.mark.asyncio
async def test_installer_wrong_target_marks_source_failed(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    source = _source(revision=1, value=1, target_resource="another-runner")
    source.workflow_id = f"wrong-target-{tmp_path.name}"
    source.module_name = canonical_generated_module_name(
        source.workflow_id, source.revision, source.source_sha256
    )
    source = await context.db.save(source)

    result = await install_generated_workflow._inner(source)

    assert result is False
    stored_source = await context.db.find_one(
        GeneratedWorkflowSource,
        GeneratedWorkflowSource.id == source.id,
    )
    assert stored_source.status == GeneratedWorkflowStatus.FAILED
    assert "another-runner" in stored_source.error
    assert not (tmp_path / "simstack_generated" / f"{source.module_name}.py").exists()


@pytest.mark.asyncio
async def test_installer_rejects_unsafe_persisted_source_before_materializing(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_GENERATED_WORKFLOW_DIR", str(tmp_path))
    source_code = (
        SOURCE_TEMPLATE.format(value=1)
        .replace(
            "from odmantic import Model",
            "from pathlib import Path\nfrom odmantic import Model",
        )
        .replace(
            "return GeneratedNumber(value=1)",
            "Path('/tmp/owned').write_text('owned')\n    return GeneratedNumber(value=1)",
        )
    )
    source = _source(revision=1, value=1)
    source.workflow_id = f"unsafe-{tmp_path.name}"
    source.source_code = source_code
    source.source_sha256 = canonical_source_sha256(source_code)
    source.module_name = canonical_generated_module_name(
        source.workflow_id, source.revision, source.source_sha256
    )
    source = await context.db.save(source)

    result = await install_generated_workflow._inner(source)

    assert result is False
    stored_source = await context.db.find_one(
        GeneratedWorkflowSource,
        GeneratedWorkflowSource.id == source.id,
    )
    assert stored_source.status == GeneratedWorkflowStatus.FAILED
    assert "pathlib" in stored_source.error
    assert not (tmp_path / "simstack_generated" / f"{source.module_name}.py").exists()
