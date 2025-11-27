from pathlib import Path

from simstack.core.node import node
from simstack.models.files import FileStack
from simstack.models.parameters import Resource

@node
def get_file(source: FileStack, local_resource: Resource, target_path: Path) -> Path:
    raise NotImplementedError("get_file is not implemented yet.")
    return target_path
