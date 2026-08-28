from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core.definitions import TaskStatus
from simstack.core.node import (
    Node,
    docker_image_for_node,
    normalize_docker_image,
    process_is_in_docker,
    should_handoff_nested_execution,
)
from simstack.models import NodeRegistry, Project
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


def _images(name, resource=None):
    if name == "dftb_calculator":
        return DFTB_IMAGE
    return PSI4_IMAGE


def _execution_node(name: str, parameters: Parameters) -> Node:
    execution_node = Node.__new__(Node)
    execution_node.name = name
    execution_node.parameters = parameters
    execution_node.registry_entry = SimpleNamespace(
        id="task-id",
        parameters=parameters,
        status=TaskStatus.SUBMITTED,
    )
    return execution_node


def _patch_run_somewhere_context(monkeypatch, *, in_docker: bool, current_node_name: str | None):
    saved_entries: list = []

    async def fake_find_one_and_update(query, update):
        saved_entries.append((query, update))
        return {}

    async def fake_claim(entry):
        if entry.status != TaskStatus.SUBMITTED:
            return False
        entry.status = TaskStatus.RETRIEVED
        return True

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=in_docker,
            current_node_name=current_node_name,
            config=SimpleNamespace(resource="local"),
            resource_config=None,
            db=SimpleNamespace(
                get_collection=lambda model: SimpleNamespace(
                    find_one_and_update=fake_find_one_and_update
                )
            ),
        ),
    )
    monkeypatch.setattr("simstack.core.node.docker_image_for_node", _images)
    monkeypatch.setattr("simstack.core.node.claim_submitted_node", fake_claim)
    monkeypatch.setattr(
        "simstack.core.node._DOCKERENV_PATH", MagicMock(exists=lambda: False)
    )
    return saved_entries


async def _publish_nested_node(
    monkeypatch,
    *,
    child_image: str | None,
    parameters: Parameters,
    nested_self: bool = False,
    process_in_docker: bool = True,
):
    visible_registry_entries: list[dict] = []
    project = Project(field_name="nested-publication-test")

    class ObservedDB:
        async def find(self, model):
            assert model is Project
            return [project]

        async def save(self, entry):
            if isinstance(entry, NodeRegistry):
                snapshot = {
                    "status": entry.status,
                    "in_docker": entry.parameters.in_docker,
                    "resource": str(entry.parameters.resource),
                    "host_claimed": entry.status == TaskStatus.SUBMITTED,
                }
                visible_registry_entries.append(snapshot)
                if snapshot["host_claimed"]:
                    entry.status = TaskStatus.RETRIEVED
            return entry

    async def keep_parameters(db, registry_entry, *, parent_parameters=None):
        return None

    def child_node():
        return None

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=process_in_docker,
            current_node_name="parent_node",
            config=SimpleNamespace(resource="local"),
            node_mappings=SimpleNamespace(
                get_by_name=lambda name: SimpleNamespace(
                    function_mapping="tests.child_node"
                )
            ),
            db=ObservedDB(),
        ),
    )
    monkeypatch.setattr(
        "simstack.core.node.process_is_in_docker", lambda: process_in_docker
    )
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE
        if name == "parent_node"
        else child_image,
    )
    monkeypatch.setattr(
        "simstack.core.node.apply_resource_assignment_to_node_registry",
        keep_parameters,
    )

    node_kwargs = dict(
        func=child_node,
        is_async=False,
        parameters=parameters,
    )
    if nested_self:
        node_kwargs["parent_parameters"] = Parameters(resource="self")
    execution_node = Node(**node_kwargs)
    await execution_node.make_registry_entry("function-hash", "arg-hash")
    return execution_node, visible_registry_entries


@pytest.mark.asyncio
async def test_same_image_nested_task_is_owned_before_first_save(monkeypatch):
    execution_node, visible_entries = await _publish_nested_node(
        monkeypatch,
        child_image=PSI4_IMAGE,
        parameters=Parameters(
            resource="local", queue="default", in_docker=True
        ),
    )

    assert visible_entries == [
        {
            "status": TaskStatus.RETRIEVED,
            "in_docker": True,
            "resource": "local",
            "host_claimed": False,
        }
    ]
    assert execution_node._owns_inline_nested_execution is True

    sentinel = SimpleNamespace(value="inline-result")

    async def fake_process():
        return sentinel

    async def forbidden_wait():
        raise AssertionError("creator-owned task must not wait for the host runner")

    async def forbidden_claim(entry):
        raise AssertionError("creator-owned task must not compete for a second claim")

    monkeypatch.setattr(execution_node, "run_node_as_process", fake_process)
    monkeypatch.setattr(
        execution_node, "_wait_for_remote_completion", forbidden_wait
    )
    monkeypatch.setattr("simstack.core.node.claim_submitted_node", forbidden_claim)

    assert await execution_node.run_somewhere() is sentinel


@pytest.mark.asyncio
async def test_different_image_handoff_is_final_before_first_save(monkeypatch):
    execution_node, visible_entries = await _publish_nested_node(
        monkeypatch,
        child_image=DFTB_IMAGE,
        parameters=Parameters(
            resource="local", queue="default", in_docker=True
        ),
    )

    assert visible_entries == [
        {
            "status": TaskStatus.SUBMITTED,
            "in_docker": True,
            "resource": "local",
            "host_claimed": True,
        }
    ]
    assert execution_node._nested_execution_handoff_prepared is True

    sentinel = SimpleNamespace(value="host-result")

    async def fake_wait():
        return sentinel

    async def forbidden_process():
        raise AssertionError("different image must execute through the host runner")

    async def forbidden_late_handoff():
        raise AssertionError("handoff must already be complete before publication")

    monkeypatch.setattr(
        execution_node, "_wait_for_remote_completion", fake_wait
    )
    monkeypatch.setattr(execution_node, "run_node_as_process", forbidden_process)
    monkeypatch.setattr(
        execution_node, "_persist_nested_execution_handoff", forbidden_late_handoff
    )

    assert await execution_node.run_somewhere() is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("in_docker", [False, True])
async def test_self_child_is_owned_before_first_save_regardless_of_docker_flag(
    monkeypatch, in_docker
):
    execution_node, visible_entries = await _publish_nested_node(
        monkeypatch,
        child_image=DFTB_IMAGE,
        parameters=Parameters(
            resource="self", queue="slurm-queue", in_docker=in_docker
        ),
        nested_self=True,
    )

    assert visible_entries == [
        {
            "status": TaskStatus.RETRIEVED,
            "in_docker": in_docker,
            "resource": "self",
            "host_claimed": False,
        }
    ]
    assert execution_node._owns_inline_nested_execution is True
    assert execution_node._nested_execution_handoff_prepared is False


@pytest.mark.asyncio
async def test_host_side_self_child_is_owned_before_first_save(monkeypatch):
    execution_node, visible_entries = await _publish_nested_node(
        monkeypatch,
        child_image=DFTB_IMAGE,
        parameters=Parameters(
            resource="self", queue="default", in_docker=False
        ),
        nested_self=True,
        process_in_docker=False,
    )

    assert visible_entries == [
        {
            "status": TaskStatus.RETRIEVED,
            "in_docker": False,
            "resource": "self",
            "host_claimed": False,
        }
    ]
    assert execution_node._owns_inline_nested_execution is True


@pytest.mark.asyncio
@pytest.mark.parametrize("child_image", [PSI4_IMAGE, DFTB_IMAGE, None])
async def test_explicit_non_docker_child_is_handed_off_without_flipping_flag(
    monkeypatch, child_image
):
    execution_node, visible_entries = await _publish_nested_node(
        monkeypatch,
        child_image=child_image,
        parameters=Parameters(
            resource="local", queue="default", in_docker=False
        ),
    )

    assert visible_entries == [
        {
            "status": TaskStatus.SUBMITTED,
            "in_docker": False,
            "resource": "local",
            "host_claimed": True,
        }
    ]
    assert execution_node.parameters.in_docker is False
    assert execution_node._nested_execution_handoff_prepared is True
    assert execution_node._owns_inline_nested_execution is False


@pytest.mark.asyncio
async def test_same_resource_docker_task_waits_for_host_runner(monkeypatch):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch, in_docker=False, current_node_name=None
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="docker-result")

    async def fake_process(self):
        raise AssertionError("host must hand a Docker task to the runner")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert len(saved_entries) == 1
    assert execution_node.registry_entry.status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_in_docker_same_image_stays_local(monkeypatch):
    _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "psi4_calculator",
        Parameters(resource="local", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="same-image")

    async def fake_process(self):
        return sentinel

    async def fake_wait(self):
        raise AssertionError("same docker image must execute in-process")

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel


@pytest.mark.asyncio
async def test_in_docker_different_image_waits_for_host(monkeypatch):
    _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="other-container")
    local_called = False

    async def fake_process(self):
        nonlocal local_called
        local_called = True
        raise AssertionError("different docker image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert local_called is False
    assert execution_node.parameters.in_docker is True
    assert execution_node.registry_entry.parameters.in_docker is True


@pytest.mark.asyncio
async def test_in_docker_non_docker_handoff_preserves_explicit_false(monkeypatch):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=False),
    )
    sentinel = SimpleNamespace(value="other-container")

    async def fake_process(self):
        raise AssertionError("different docker image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert execution_node.parameters.in_docker is False
    assert len(saved_entries) == 1
    assert execution_node.registry_entry.parameters.in_docker is False
    assert saved_entries[0][1]["$set"]["parameters.in_docker"] is False


@pytest.mark.asyncio
async def test_in_docker_missing_child_image_waits_for_host(monkeypatch):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    monkeypatch.setattr(
        "simstack.core.node.docker_image_for_node",
        lambda name, resource=None: PSI4_IMAGE if name == "multistep_optimizer" else None,
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="missing-image-wait")

    async def fake_process(self):
        raise AssertionError("missing child image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert execution_node.parameters.in_docker is True
    assert len(saved_entries) == 1


@pytest.mark.asyncio
async def test_self_resource_with_different_image_waits_for_host(monkeypatch):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="self", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="self-resource")

    async def fake_process(self):
        raise AssertionError("different image must return to the host")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert len(saved_entries) == 1
    assert execution_node.registry_entry.status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize("in_docker", [False, True])
async def test_nested_self_resource_stays_in_current_execution_context(
    monkeypatch, in_docker
):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch,
        in_docker=True,
        current_node_name="multistep_optimizer",
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="self", queue="slurm-queue", in_docker=in_docker),
    )
    execution_node.parent_id = "parent-task"
    execution_node._function_kwargs = {}
    sentinel = SimpleNamespace(value="same-context")

    async def fake_process(self):
        return sentinel

    async def forbidden_wait(self):
        raise AssertionError("nested self resource must stay in the current context")

    monkeypatch.setattr(Node, "run_node_as_process", fake_process)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", forbidden_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert saved_entries == []
    assert execution_node.registry_entry.status == TaskStatus.RETRIEVED
