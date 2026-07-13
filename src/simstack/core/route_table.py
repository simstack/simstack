from typing import List, Dict


class RouteTable:
    _instance = None

    def __init__(self):
        if RouteTable._instance is not None:
            raise RuntimeError("Use get_instance() instead")
        self.targets = {}

    @classmethod
    def get_instance(cls) -> 'RouteTable':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def add_route_set(self, source: str, targets: List[str]) -> None:
        self.targets[source] = targets

    def clear_routes(self) -> None:
        self.targets.clear()

route_table = RouteTable.get_instance()
