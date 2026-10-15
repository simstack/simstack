from typing import Dict, List, Optional, Tuple

from simstack.models.models import ModelMapping, NodeModel


class ModelMappingTable:
    def __init__(self, mappings: List[ModelMapping]):
        self._by_name: Dict[str, ModelMapping] = {m.name: m for m in mappings}
        self._by_mapping: Dict[str, ModelMapping] = {m.mapping: m for m in mappings}

    def get_by_name(self, name: str) -> Optional[ModelMapping]:
        return self._by_name.get(name)

    def get_by_mapping(self, mapping: str) -> Optional[ModelMapping]:
        return self._by_mapping.get(mapping)

    def all_mappings(self) -> Tuple[ModelMapping, ...]:
        return tuple(self._by_mapping.values())


class NodeMappingTable:
    def __init__(self, nodes: List[NodeModel]):
        self._by_name: Dict[str, NodeModel] = {n.name: n for n in nodes}
        self._by_mapping: Dict[str, NodeModel] = {n.function_mapping: n for n in nodes}

    def get_by_name(self, name: str) -> Optional[NodeModel]:
        return self._by_name.get(name)

    def get_by_mapping(self, mapping: str) -> Optional[NodeModel]:
        return self._by_mapping.get(mapping)
