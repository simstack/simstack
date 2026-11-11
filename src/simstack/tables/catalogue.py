"""Local catalogue discovery output; persistence belongs to Server."""


class CatalogueCollector:
    def __init__(self, models=(), nodes=()):
        self.models = {model.mapping: model for model in models}
        self.model_types = {}
        self.nodes = {node.function_mapping: node for node in nodes}

    def register_model(self, descriptor, *, model_class=None):
        for mapping, previous in list(self.models.items()):
            if previous.name == descriptor.name and mapping != descriptor.mapping:
                del self.models[mapping]
        self.models[descriptor.mapping] = descriptor
        if model_class is not None:
            self.model_types[descriptor.mapping] = model_class
        return descriptor

    def register_node(self, descriptor):
        for mapping, previous in list(self.nodes.items()):
            if previous.name == descriptor.name and mapping != descriptor.function_mapping:
                del self.nodes[mapping]
        self.nodes[descriptor.function_mapping] = descriptor
        return descriptor

    def model_by_mapping(self, mapping):
        return self.models.get(mapping)

    def model_by_name(self, name):
        matches = [model for model in self.models.values() if model.name == name]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous model catalogue name: {name}")
        return matches[0] if matches else None

    def nodes_by_name(self, name):
        return [node for node in self.nodes.values() if node.name == name]
