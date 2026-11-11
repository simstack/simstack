from typing import Dict, Optional, List
from simstack.models.models import ModelMapping, NodeModel
from typing import Any

class ModelMappingTable:
    def __init__(self, mappings: List[ModelMapping]):
        self._by_name: Dict[str, ModelMapping] = {m.name: m for m in mappings}
        self._by_mapping: Dict[str, ModelMapping] = {m.mapping: m for m in mappings}

    def get_by_name(self, name: str) -> Optional[ModelMapping]:
        return self._by_name.get(name)

    def get_by_mapping(self, mapping: str) -> Optional[ModelMapping]:
        return self._by_mapping.get(mapping)

    @classmethod
    async def load(cls, api: Any):
        mappings = await api.catalogue_models()
        return cls(list(mappings))

class NodeMappingTable:
    def __init__(self, nodes: List[NodeModel]):
        self._by_name: Dict[str, NodeModel] = {n.name: n for n in nodes}
        self._by_mapping: Dict[str, NodeModel] = {n.function_mapping: n for n in nodes}

    def get_by_name(self, name: str) -> Optional[NodeModel]:
        return self._by_name.get(name)

    def get_by_mapping(self, mapping: str) -> Optional[NodeModel]:
        return self._by_mapping.get(mapping)

    @classmethod
    async def load(cls, api: Any):
        return cls(list(await api.catalogue_nodes()))
