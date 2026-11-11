from typing import List, Tuple, Union

from simstack.models.document import Model
from simstack.core.simstack_result import SimstackResult
from simstack.models import BooleanData
from simstack.models.files import FileStack
from simstack.core.context import context
from simstack.models.file_list import FileListModel
from simstack.models.simstack_model import is_simstack_model
from simstack.models.named_data_reference import NamedDataReference


async def process_result_helper(
    result: Union[SimstackResult, Model, bool, BooleanData], task_id: str = "NA",
) -> Tuple[List[NamedDataReference], List[Model]]:
    """Collect outputs without republishing or changing an existing child result."""
    named = []
    if isinstance(result, (bool, BooleanData)):
        named.append(("value", BooleanData(value=result) if isinstance(result, bool) else result))
    elif isinstance(result, SimstackResult):
        if result.files:
            files = FileListModel()
            for file in result.files:
                if not isinstance(file, FileStack):
                    raise ValueError(f"task_id: {task_id} cannot save file: FileStack expected but got {type(file)}")
                files.append(file)
            named.append(("files", files))
        for name, value in (result.model_extra or {}).items():
            if not name.startswith("_") and not callable(value) and is_simstack_model(value):
                if not isinstance(value, Model):
                    raise ValueError(f"task_id: {task_id} cannot save model: {name} is not a model")
                named.append((name, value))
    elif is_simstack_model(result) and isinstance(result, Model):
        named.append((result.__class__.__name__, result))
    references = [NamedDataReference.from_variable(value, variable_name=name, task_id=task_id)
                  for name, value in named]
    return references, [value for _, value in named]
