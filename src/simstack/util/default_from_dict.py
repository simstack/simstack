import logging
from typing import Any

from odmantic import Model

logger = logging.getLogger(__name__)


def default_from_model(cls_method, model: Model, **kwargs) -> Model:
    model_dict = model.model_dump(exclude={"id"})
    return cls_method.from_dict(model_dict, **kwargs)


def default_from_dict(cls_method, data: dict, **kwargs) -> Any:
    """
    Create an instance of the model from a dictionary.
    Handles nested models and enum values.
    """
    if not isinstance(data, dict):
        return data  # Return as is if it's not a dictionary

    processed_data = {}
    for field_name, field in cls_method.model_fields.items():
        if field_name not in data:
            continue

        field_type = field.annotation
        field_value = data[field_name]

        # Handle enum values
        if hasattr(field_type, "__members__"):
            logger.debug("Handling enum field: %s", field_name)
            if isinstance(field_value, field_type):
                # If it's already an enum instance, use it directly
                logger.debug("Field value is enum instance: %s", field_value)
                processed_data[field_name] = field_value
            else:
                # Try to create enum from the value
                try:
                    processed_data[field_name] = field_type(field_value)
                    logger.debug(
                        "Converted field value to enum: %s", processed_data[field_name]
                    )
                except ValueError:
                    logger.debug(
                        "Invalid enum value for field %s: %s", field_name, field_value
                    )
                    raise ValueError(
                        f"Invalid enum value '{field_value}' for field {field_name}"
                    )
        # Handle nested models
        elif hasattr(field_type, "from_dict") and isinstance(field_value, dict):
            processed_data[field_name] = field_type.from_dict(field_value)
        else:
            processed_data[field_name] = field_value

    return cls_method.model_validate(processed_data)
