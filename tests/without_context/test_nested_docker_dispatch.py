from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node import (
    Node,
    docker_image_for_node,
    normalize_docker_image,
    process_is_in_docker,
    should_handoff_nested_execution,
)
from simstack.models import NodeRegistry
from simstack.models.document import ObjectId
from simstack.runner_runtime.launch_claims import current_execution, pending, reserve_launch, task_execution
from simstack.models.parameters import Parameters
from simstack.util.resource_config import ResourceConfig


PSI4_IMAGE = "docker.io/library/molecular-qm-psi4:latest"
DFTB_IMAGE = "docker.io/library/molecular-qm-dftb:latest"


def test_normalize_docker_image_strips_hub_library_prefix():
    assert normalize_docker_image(PSI4_IMAGE) == "molecular-qm-psi4:latest"
    assert normalize_docker_image("molecular-qm-psi4:latest") == "molecular-qm-psi4:latest"
    assert normalize_docker_image("docker.io/simstack/molecular-qm-psi4:latest") == (
        "simstack/molecular-qm-psi4:latest"
    )
    assert normalize_docker_image(None) is None
    assert normalize_docker_image("  ") is None


def test_docker_image_for_node_looks_up_program(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[local.program.dftb_calculator]\ndocker_image = "{DFTB_IMAGE}"\n'
        f'[local.program.multistep_optimizer]\ndocker_image = "{PSI4_IMAGE}"\n'
    )
    resource_config = ResourceConfig(config_file, "local")
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            resource_config=resource_config,
            config=SimpleNamespace(resource="local"),
        ),
    )

    assert docker_image_for_node("dftb_calculator") == DFTB_IMAGE
    assert docker_image_for_node("multistep_optimizer", "local") == PSI4_IMAGE
    assert docker_image_for_node("dftb_calculator", "remote") is None
    assert docker_image_for_node("missing_node") is None


def test_docker_image_for_node_maps_self_to_current_resource(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[cluster.program.dftb_calculator]\ndocker_image = "{DFTB_IMAGE}"\n'
    )
    resource_config = ResourceConfig(config_file, "cluster")
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            resource_config=resource_config,
            config=SimpleNamespace(resource="cluster"),
        ),
    )

    assert docker_image_for_node("dftb_calculator", "self") == DFTB_IMAGE


def test_should_dispatch_true_on_host_when_task_requires_docker(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: False)
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_handoff_nested_execution(params, "dftb_calculator") is True


def test_should_handoff_when_container_child_explicitly_disables_docker(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: DFTB_IMAGE if name == "dftb_calculator" else PSI4_IMAGE,
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="multistep_optimizer",
            config=SimpleNamespace(resource="local"),
        ),
    )
    params = Parameters(resource="local", queue="default", in_docker=False)
    assert should_handoff_nested_execution(params, "dftb_calculator") is True


def test_should_dispatch_true_when_child_image_missing(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE if name == "multistep_optimizer" else None,
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="multistep_optimizer",
            config=SimpleNamespace(resource="local"),
        ),
    )
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_handoff_nested_execution(params, "dftb_calculator") is True


def test_should_dispatch_false_when_same_image(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE,
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="multistep_optimizer",
            config=SimpleNamespace(resource="local"),
        ),
    )
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_handoff_nested_execution(params, "psi4_calculator") is False


def test_separate_cloud_vm_hands_off_even_when_the_image_matches(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE,
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="pyscf_hessian",
            config=SimpleNamespace(resource="cloud"),
        ),
    )
    params = Parameters(
        resource="cloud", queue="default", in_docker=True, separate_cloud_vm=True
    )
    assert should_handoff_nested_execution(params, "pyscf_hessian_for_atoms") is True


def test_should_dispatch_true_when_images_differ(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: (
            DFTB_IMAGE if name == "dftb_calculator" else PSI4_IMAGE
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="multistep_optimizer",
            config=SimpleNamespace(resource="local"),
        ),
    )
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_handoff_nested_execution(params, "dftb_calculator") is True


def test_should_dispatch_true_when_hub_prefix_differs_but_image_same(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: True)

    def fake_image(name, resource=None):
        if name == "multistep_optimizer":
            return PSI4_IMAGE
        return "molecular-qm-psi4:latest"

    monkeypatch.setattr("simstack.core.node.docker_image_for_node", fake_image)
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            current_node_name="multistep_optimizer",
            config=SimpleNamespace(resource="local"),
        ),
    )
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_handoff_nested_execution(params, "psi4_calculator") is False


def test_process_is_in_docker_reads_context_flag(monkeypatch):
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(in_docker=True),
    )
    monkeypatch.setattr("simstack.core.node._DOCKERENV_PATH", MagicMock(exists=lambda: False))
    assert process_is_in_docker() is True


def test_process_is_in_docker_false_without_flag_or_dockerenv(monkeypatch):
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(in_docker=False),
    )
    monkeypatch.setattr("simstack.core.node._DOCKERENV_PATH", MagicMock(exists=lambda: False))
    assert process_is_in_docker() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("resource,queue,in_docker,process_in_docker,current_resource,child_image,expected_inline", [
    ("local", "default", True, True, "local", PSI4_IMAGE, True),
    ("local", "default", True, True, "local", DFTB_IMAGE, False),
    ("local", "default", False, True, "local", PSI4_IMAGE, False),
    ("local", "default", False, True, "local", DFTB_IMAGE, False),
    ("local", "default", False, True, "local", None, False),
    ("local", "default", True, False, "local", PSI4_IMAGE, False),
    ("local", "default", True, True, "local", None, False),
    ("self", "slurm-queue", False, True, "local", DFTB_IMAGE, True),
    ("self", "slurm-queue", True, True, "local", DFTB_IMAGE, True),
    ("self", "default", False, False, "local", DFTB_IMAGE, True),
    ("cloud", "default", True, True, "cloud", PSI4_IMAGE, False),
    ("self", "default", True, True, "cloud", PSI4_IMAGE, True),
])
async def test_nested_publication_hints_keep_container_and_cloud_semantics(
    monkeypatch, tmp_path, resource, queue, in_docker, process_in_docker,
    current_resource, child_image, expected_inline,
):
    monkeypatch.setenv("SIMSTACK_RUNNER_WORKDIR", str(tmp_path))
    calls = []
    input_publication = str(uuid4())
    api = SimpleNamespace(requests=SimpleNamespace(server_url="https://example.invalid/api",
        resource=current_resource, authorization="runner", bootstrap_data={"account_id": "test-account"}),
        values=SimpleNamespace(publish_task_inputs=AsyncMock(return_value=([], input_publication))),
        tasks=SimpleNamespace())
    mapping = SimpleNamespace(function_mapping="tests.child_node", repository_revision=None)
    parent = SimpleNamespace(id=ObjectId(), _execution_claim_id="parent-claim", _execution_generation="parent-generation")
    active = SimpleNamespace(server=api, in_docker=process_in_docker,
        current_node_name="parent_node", config=SimpleNamespace(resource=current_resource),
        node_mappings=SimpleNamespace(get_by_name=lambda name: mapping))
    monkeypatch.setattr("simstack.core.node.context", active)
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: process_in_docker)
    monkeypatch.setattr("simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE if name == "parent_node" else child_image)
    async def publish(payload, *, request_id):
        assert payload["input_publication"] == input_publication
        assert payload["inline"] is expected_inline
        assert payload["parent_execution"] == current_execution()
        assert pending(api).__next__()["publication"] == payload
        assert reserve_launch(api, payload["requested_task_id"]) is None
        calls.append(payload)
        entry = NodeRegistry(id=payload["requested_task_id"], name="child_node",
            status=TaskStatus.RETRIEVED if expected_inline else TaskStatus.SUBMITTED,
            function_hash="", arg_hash="hash", func_mapping="tests.child_node",
            parameters=Parameters.model_validate(payload["parameters"]))
        object.__setattr__(entry, "_execution_claim_id", "canonical-claim")
        object.__setattr__(entry, "_execution_generation", "canonical-generation")
        return {"task": entry, "disposition": "launch_inline" if expected_inline else "scheduled", "recompute_artifacts": False}
    api.tasks.create_child = publish
    async def resolve(payload, *, request_id):
        effective = Parameters.model_validate(payload["parameters"])
        logical_self = effective.resource == "self"
        if logical_self:
            effective.resource = current_resource
        return {"parameters": effective.model_dump(mode="json"),
            "logical_self": logical_self, "route": "resolved-route", "repository_revision": None}
    async def reuse(payload, *, request_id):
        return {"task": None, "disposition": "cache_miss", **await resolve(payload, request_id=request_id)}
    api.tasks.resolve_invocation = AsyncMock(side_effect=resolve)
    api.tasks.reuse_invocation = reuse
    def child_node(**kwargs):
        return True
    value = Node(func=child_node, is_async=False, parent_id=parent.id,
        parameters=Parameters(resource=resource, queue=queue, in_docker=in_docker))
    with task_execution(parent):
        await value.get_node_registry()
    api.tasks.resolve_invocation.assert_awaited_once()
    api.values.publish_task_inputs.assert_awaited_once_with([], repository_revision=None)
    assert value.parameters.in_docker is in_docker
    assert value._owns_inline_nested_execution is expected_inline
    sentinel = object()
    value.run_node_as_process = AsyncMock(return_value=sentinel)
    value._wait_for_remote_completion = AsyncMock(return_value=sentinel)
    assert await value.run_somewhere() is sentinel
    if expected_inline:
        value.run_node_as_process.assert_awaited_once()
        value._wait_for_remote_completion.assert_not_awaited()
    else:
        value._wait_for_remote_completion.assert_awaited_once()
        value.run_node_as_process.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("granted_inline", [False, True])
async def test_execution_uses_server_disposition_without_a_second_claim(monkeypatch, granted_inline):
    node = Node.__new__(Node)
    node._owns_inline_nested_execution = granted_inline
    node._inline_launch_reservation = None
    inline_result, scheduled_result = object(), object()
    node.run_node_as_process = AsyncMock(return_value=inline_result)
    node._wait_for_remote_completion = AsyncMock(return_value=scheduled_result)
    assert await node.run_somewhere() is (inline_result if granted_inline else scheduled_result)
    assert node.run_node_as_process.await_count == int(granted_inline)
    assert node._wait_for_remote_completion.await_count == int(not granted_inline)
