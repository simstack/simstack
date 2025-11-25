from typing import Dict, Any


async def custom_model_dump(self, **kwargs) -> Dict[str, Any]:
    """
    Custom model dump method to handle the conversion of model instances to dictionaries.
    This method recursively traverses dictionaries and lists to convert any nested
    model instances to their dictionary representation.

    :param self: The model instance
    :param kwargs: Additional keyword arguments
    :return: A dictionary representation of the model instance
    """
    # First get the dictionary from the standard model_dump

    filtered_kwargs = {k: v for k, v in kwargs.items() if k != "engine"}
    data = self.model_dump(**filtered_kwargs)

    # Process each field - the key is to process the original values from self
    for field_name, field in self.model_fields.items():
        field_value = getattr(self, field_name)

        # Skip None values
        if field_value is None:
            continue

        data[field_name] = await _recursive_process(field_value, **kwargs)

    return data


async def _recursive_process(obj, **kwargs):
    """
    Helper function to recursively process an object that might contain models
    """
    if hasattr(obj, "custom_model_dump"):
        return await obj.custom_model_dump(**kwargs)
    elif isinstance(obj, dict):
        return {k: await _recursive_process(v, **kwargs) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [await _recursive_process(item, **kwargs) for item in obj]
    else:
        return obj


async def _process_container(container, **kwargs):
    """
    Helper function to recursively process dictionaries and lists,
    converting any model instances to their dictionary representation.

    :param container: The container to process (dict or list)
    :param kwargs: Additional keyword arguments
    :return: Processed container with model instances converted to dictionaries
    """
    if isinstance(container, dict):
        result = {}
        for k, v in container.items():
            if hasattr(v, "custom_model_dump"):
                result[k] = await v.custom_model_dump(**kwargs)
            elif isinstance(v, (dict, list)):
                result[k] = await _process_container(v, **kwargs)
            else:
                result[k] = v
        return result
    elif isinstance(container, list):
        result = []
        for item in container:
            if hasattr(item, "custom_model_dump"):
                result.append(await item.custom_model_dump(**kwargs))
            elif isinstance(item, (dict, list)):
                result.append(await _process_container(item, **kwargs))
            else:
                result.append(item)
        return result
    return container
