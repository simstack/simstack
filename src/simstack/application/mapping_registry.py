"""Load mapping registries through the non-runner persistence port."""

from simstack.models.models import ModelMapping, NodeModel
from simstack.persistence import Repository
from simstack.util.mappings import ModelMappingTable, NodeMappingTable


async def load_model_mapping_table(
    repository: Repository,
) -> ModelMappingTable:
    mappings = await repository.find(ModelMapping)
    return ModelMappingTable(list(mappings))


async def load_node_mapping_table(
    repository: Repository,
) -> NodeMappingTable:
    nodes = await repository.find(NodeModel)
    return NodeMappingTable(list(nodes))
