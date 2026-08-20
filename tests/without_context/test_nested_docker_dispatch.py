from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from simstack.core.node import (
    Node,
    docker_image_for_node,
    normalize_docker_image,
    process_is_in_docker,
    should_dispatch_nested_docker,
)
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
    assert docker_image_for_node("missing_node") is None


def test_docker_image_for_node_maps_self_to_local(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[local.program.dftb_calculator]\ndocker_image = "{DFTB_IMAGE}"\n'
    )
    resource_config = ResourceConfig(config_file, "self")
    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            resource_config=resource_config,
            config=SimpleNamespace(resource="self"),
        ),
    )

    assert docker_image_for_node("dftb_calculator", "self") == DFTB_IMAGE


def test_should_dispatch_false_when_not_in_docker(monkeypatch):
    monkeypatch.setattr("simstack.core.node.process_is_in_docker", lambda: False)
    params = Parameters(resource="local", queue="default", in_docker=True)
    assert should_dispatch_nested_docker(params, "dftb_calculator") is False


def test_should_dispatch_true_when_assignment_not_in_docker(monkeypatch):
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
    assert should_dispatch_nested_docker(params, "dftb_calculator") is True


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
    params = Parameters(resource="local", queue="default", in_docker=False)
    assert should_dispatch_nested_docker(params, "dftb_calculator") is True


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
    assert should_dispatch_nested_docker(params, "psi4_calculator") is False


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
    assert should_dispatch_nested_docker(params, "dftb_calculator") is True


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
    assert should_dispatch_nested_docker(params, "psi4_calculator") is False


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
    execution_node.registry_entry = SimpleNamespace(id="task-id", parameters=parameters)
    return execution_node


def _patch_run_somewhere_context(monkeypatch, *, in_docker: bool, current_node_name: str | None):
    saved_entries: list = []

    async def fake_save(entry):
        saved_entries.append(entry)

    monkeypatch.setattr(
        "simstack.core.node.context",
        SimpleNamespace(
            in_docker=in_docker,
            current_node_name=current_node_name,
            config=SimpleNamespace(resource="local"),
            resource_config=None,
            db=SimpleNamespace(save=fake_save),
        ),
    )
    monkeypatch.setattr("simstack.core.node.docker_image_for_node", _images)
    monkeypatch.setattr(
        "simstack.core.node._DOCKERENV_PATH", MagicMock(exists=lambda: False)
    )
    return saved_entries


@pytest.mark.asyncio
async def test_same_resource_default_queue_stays_local_when_not_in_docker(monkeypatch):
    _patch_run_somewhere_context(monkeypatch, in_docker=False, current_node_name=None)
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="local-result")
    wait_called = False

    async def fake_local(self):
        return sentinel

    async def fake_wait(self):
        nonlocal wait_called
        wait_called = True
        raise AssertionError("host process must keep same-resource default-queue in-process")

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert wait_called is False


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

    async def fake_local(self):
        return sentinel

    async def fake_wait(self):
        raise AssertionError("same docker image must execute in-process")

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
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

    async def fake_local(self):
        nonlocal local_called
        local_called = True
        raise AssertionError("different docker image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert local_called is False
    assert execution_node.parameters.in_docker is True
    assert execution_node.registry_entry.parameters.in_docker is True


@pytest.mark.asyncio
async def test_in_docker_waits_and_persists_in_docker_when_assignment_false(monkeypatch):
    saved_entries = _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="local", queue="default", in_docker=False),
    )
    sentinel = SimpleNamespace(value="other-container")

    async def fake_local(self):
        raise AssertionError("different docker image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert execution_node.parameters.in_docker is True
    assert len(saved_entries) == 1
    assert saved_entries[0].parameters.in_docker is True


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
        Parameters(resource="local", queue="default", in_docker=False),
    )
    sentinel = SimpleNamespace(value="missing-image-wait")

    async def fake_local(self):
        raise AssertionError("missing child image must not execute in-process")

    async def fake_wait(self):
        return sentinel

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel
    assert execution_node.parameters.in_docker is True
    assert len(saved_entries) == 1


@pytest.mark.asyncio
async def test_self_resource_stays_local_even_when_images_differ(monkeypatch):
    _patch_run_somewhere_context(
        monkeypatch, in_docker=True, current_node_name="multistep_optimizer"
    )
    execution_node = _execution_node(
        "dftb_calculator",
        Parameters(resource="self", queue="default", in_docker=True),
    )
    sentinel = SimpleNamespace(value="self-resource")

    async def fake_local(self):
        return sentinel

    async def fake_wait(self):
        raise AssertionError("resource=self must stay in-process")

    monkeypatch.setattr(Node, "execute_node_locally", fake_local)
    monkeypatch.setattr(Node, "_wait_for_remote_completion", fake_wait)

    result = await execution_node.run_somewhere()

    assert result is sentinel

