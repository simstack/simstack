from typing import Dict, Optional, List
from simstack.models.models import ModelMapping, NodeModel
from odmantic import AIOEngine

class ModelMappingTable:
    def __init__(self, mappings: List[ModelMapping]):
        self._by_name: Dict[str, ModelMapping] = {m.name: m for m in mappings}
        self._by_mapping: Dict[str, ModelMapping] = {m.mapping: m for m in mappings}

    def get_by_name(self, name: str) -> Optional[ModelMapping]:
        return self._by_name.get(name)

    def get_by_mapping(self, mapping: str) -> Optional[ModelMapping]:
        return self._by_mapping.get(mapping)

    @classmethod
    async def load(cls, engine: AIOEngine):
        mappings = await engine.find(ModelMapping)
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
    async def load(cls, engine: AIOEngine):
        try:
            nodes = await engine.find(NodeModel)
        except Exception as e:
            print(f"Error loading NodeModel from database: {e}")
            print("WARNING: Returning empty NodeMappingTable to fix obviously corrupted case.")
            print("Rebuild the NodeMappingTable by rerunning the database initialization or migration scripts.")
            nodes = []
        return cls(list(nodes))
