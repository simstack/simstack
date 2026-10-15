import pytest

from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.core.node import compute_arg_hash, node
from simstack.core.node_code_version import node_code_version
from simstack.core.node_persistence import reconstruct_persisted_node
from simstack.models import FloatData, ModelMapping, NamedDataReference, NodeRegistry
from simstack.models.parameters import Parameters
from simstack.persistence import eq


@node
def persisted_probe(value: FloatData, **kwargs) -> FloatData:
    return FloatData(value=value.value + 1)


async def _registry(repository, value: FloatData) -> NodeRegistry:
    await repository.save(value)
    mapping = await repository.find_one(ModelMapping, eq("name", "FloatData"))
    assert mapping is not None
    return NodeRegistry(
        name="persisted_probe",
        status=TaskStatus.COMPLETED,
        input_references=[
            NamedDataReference(
                variable_name="value",
                variable_mapping=mapping.mapping,
                reference=value.id,
            )
        ],
        function_hash=node_code_version(persisted_probe._inner),
        arg_hash=compute_arg_hash([value]),
        func_mapping=f"{__name__}.persisted_probe",
        parameters=Parameters(),
    )


@pytest.mark.asyncio
async def test_reconstruct_persisted_node_is_strict_and_repository_injected(
    repository,
):
    value = FloatData(value=3.5)
    registry = await _registry(repository, value)

    reconstructed = await reconstruct_persisted_node(registry, repository)

    assert reconstructed.registry_entry is registry
    assert reconstructed.repository is repository
    assert reconstructed._args == [value]
    assert reconstructed._func is persisted_probe._inner


@pytest.mark.asyncio
async def test_reconstruct_persisted_node_rejects_missing_input(repository):
    value = FloatData(value=3.5)
    registry = await _registry(repository, value)
    await repository.delete(value)

    with pytest.raises(ValueError, match="references missing input"):
        await reconstruct_persisted_node(registry, repository)


@pytest.mark.asyncio
async def test_reconstruct_persisted_node_rejects_hash_mismatch(repository):
    value = FloatData(value=3.5)
    registry = await _registry(repository, value)
    registry.arg_hash = "wrong"

    with pytest.raises(ValueError, match="argument hash"):
        await reconstruct_persisted_node(registry, repository)
