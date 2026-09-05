from typing import Any, Tuple, List, Union
from odmantic import ObjectId, Model
from simstack.core.simstack_result import SimstackResult
from simstack.models import BooleanData
from simstack.models.files import FileStack

from simstack.core.context import context
from simstack.models import ModelMapping
from simstack.models.file_list import FileListModel
from simstack.models.simstack_model import is_simstack_model

from simstack.models.named_data_reference import NamedDataReference

import logging
logger = logging.getLogger("process_results")

async def process_result_helper(
    result: Union[SimstackResult,Model, bool, BooleanData], task_id: str = "NA"
) -> Tuple[List[NamedDataReference], List[Model]]:
    """
    Computes the results_references and returns a List[Model].
    It works if the result is a SimstackResult, a single Model or a bool.
    """

    results_references: List[NamedDataReference] = []
    result_models: List[Model] = []

    if isinstance(result, bool) or isinstance(result, BooleanData) :
        # Convert bool to BooleanData
        if isinstance(result, bool):
            boolean_data = BooleanData(value=result)
        else:
            boolean_data = result
        result_model = await context.db.save(boolean_data)
        result_models.append(result_model)
        results_references.append(NamedDataReference.from_variable(
            boolean_data,
            variable_name="value",
            task_id=task_id
        ))

        return results_references, result_models

    if isinstance(result, SimstackResult):
        # check if there are files in the result
        if len(result.files) > 0:
            file_list_model = FileListModel()
            # this goes into the results must be a model
            for file_stack in result.files:
                if file_stack:
                    if isinstance(file_stack, FileStack):
                        logger.info(f"Task task_id: {task_id} saving file: {file_stack.name} {file_stack.id}")
                        file_list_model.append(file_stack)
                    else:
                        logger.error(f"Task task_id: {task_id} cannot save file: FileStack expected but got {file_stack}")
                        raise ValueError(
                            f"Task task_id: {task_id} cannot save file: FileStack expected but got {type(file_stack)}"
                        )
                else:
                    logger.error(f"Task task_id: {task_id} saving file is NONE")
                    raise ValueError("saving file is NONE")

            saved = await context.db.save(file_list_model)
            results_references.append(NamedDataReference.from_variable(
                saved,
                variable_name="files",
                task_id=task_id
            ))
            result_models.append(saved)

        extra = getattr(result, "__pydantic_extra__", {})
        if extra is None:
            extra = {}
        for key, value in extra.items():
            # gather only non-callable public attributes
            if (
                not key.startswith("_")
                and not callable(value)
                and is_simstack_model(value)
            ):
                if isinstance(value, Model):
                    try:
                        result_model = await context.db.save(value)
                    except Exception as e:
                        logger.error(f"task_id: {task_id} cannot save model: {key} {str(e)}")
                        raise e
                    result_models.append(result_model)
                    results_references.append(NamedDataReference.from_variable(
                        result_model,
                        variable_name=key,
                        task_id=task_id
                    ))
                else:
                    raise ValueError(
                        f"task_id: {task_id} cannot save model: {key} is not a model"
                    )
    elif is_simstack_model(result) and isinstance(result, Model):
        result_model = await context.db.save(result)
        result_models.append(result_model)
        results_references.append(NamedDataReference.from_variable(
            result_model,
            variable_name=result.__class__.__name__,
            task_id=task_id
        ))

    return results_references, result_models
