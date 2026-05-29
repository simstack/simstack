from typing import Any, Tuple, List
from odmantic import ObjectId, Model
from simstack.core.simstack_result import SimstackResult
from simstack.models import BooleanData
from simstack.models.files import FileStack

from simstack.core.context import context
from simstack.models import ModelMapping
from simstack.models.file_list import FileListModel
from simstack.models.simstack_model import is_simstack_model

import logging
logger = logging.getLogger("process_results")

async def process_result_helper(
    result: Any, task_id: str = "NA"
) -> Tuple[List[ObjectId], List[str], List[Model], List[str]]:
    """
    Computes the result_ids, result_tables and result_names and returns a List[Model].
    It works if the result is a SimstackResult, a single Model or a bool.
    """

    result_ids: List[ObjectId] = []
    result_tables: List[str] = []
    result_models: List[Model] = []
    result_names: List[str] = []

    if isinstance(result, bool):
        # Convert bool to BooleanData
        boolean_data = BooleanData(value=result)
        result_model = await context.db.upsert(boolean_data)
        result_models.append(result_model)
        if result_model.id is None:
            raise ValueError(
                f"Task task_id: {task_id} saved BooleanData has no ID"
            )
        result_ids.append(result_model.id)
        result_names.append("value")
        result_table_name = await context.db.find_one(
            ModelMapping, ModelMapping.name == BooleanData.__name__
        )
        if result_table_name is None:
            logger.error(
                f"Task task_id: {task_id} could not find table name for {BooleanData.__name__}"
            )
            raise ValueError(
                f"Could not find table name for {BooleanData.__name__}"
            )
        result_tables.append(result_table_name.mapping)
        return result_ids, result_tables, result_models, result_names

    if isinstance(result, SimstackResult):
        # check if there are files in the result
        if len(result.files) > 0:
            file_list_model = FileListModel()
            # this goes into the results must be a model
            for file_stack in result.files:
                if file_stack:
                    if isinstance(file_stack, FileStack):
                        logger.info(
                            f"Task task_id: {task_id} saving file: {file_stack.name} {file_stack.id}"
                        )
                        saved = await context.db.save(file_stack)
                        await file_list_model.append(saved)
                    else:
                        logger.error(
                            f"Task task_id: {task_id} cannot save file: FileStack expected but got {file_stack}"
                        )
                        raise ValueError(
                            f"Task task_id: {task_id} cannot save file: FileStack expected but got {type(file_stack)}"
                        )
                else:
                    logger.error(f"Task task_id: {task_id} saving file is NONE")
                    raise ValueError("saving file is NONE")

            result_table_name = await context.db.find_one(
                ModelMapping, ModelMapping.name == FileListModel.__name__
            )
            if result_table_name is None:
                logger.error(
                    f"Task task_id: {task_id} could not find table name for {FileListModel.__name__}"
                )
                raise ValueError(
                    f"Could not find table name for {FileListModel.__name__}"
                )
            result_tables.append(result_table_name.mapping)
            result_names.append("files")
            saved = await context.db.save(file_list_model)
            if saved.id is None:
                raise ValueError(f"Task task_id: {task_id} saved FileListModel has no ID")
            result_ids.append(saved.id)
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
                    result_model = await context.db.upsert(value)
                    result_models.append(result_model)
                    if result_model.id is None:
                        raise ValueError(
                            f"Task task_id: {task_id} saved model {key} has no ID"
                        )
                    result_ids.append(result_model.id)
                    result_names.append(key)
                    result_table_name = await context.db.find_one(
                        ModelMapping, ModelMapping.name == value.__class__.__name__
                    )
                    if result_table_name is None:
                        logger.error(
                            f"Task task_id: {task_id} could not find table name for {value.__class__.__name__}"
                        )
                        raise ValueError(
                            f"Could not find table name for {value.__class__.__name__}"
                        )
                    result_tables.append(result_table_name.mapping)
                else:
                    raise ValueError(
                        f"task_id: {task_id} cannot save model: {key} is not a model"
                    )
    elif is_simstack_model(result) and isinstance(result, Model):
        result_model = await context.db.upsert(result)
        result_models.append(result_model)
        result_ids.append(result_model.id)
        result_names.append(result.__class__.__name__)
        result_table_name = await context.db.find_one(
            ModelMapping, ModelMapping.name == result.__class__.__name__
        )
        if result_table_name is None:
            logger.error(
                f"Task task_id: {task_id} could not find table name for {result.__class__.__name__}"
            )
            raise ValueError(
                f"Could not find table name for {result.__class__.__name__}"
            )
        result_tables.append(result_table_name.mapping)

    return result_ids, result_tables, result_models, result_names
