from typing import Dict, Any


def filter_file_content(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively searches through nested data structures to filter out sensitive content.
    Specifically looks for 'content' fields in dictionaries, especially within 'files' structures.

    Args:
        data: Any Python data structure (dict, list, etc.)

    Returns:
        The filtered data structure with sensitive content removed
    """
    if isinstance(data, dict):
        # Create a new dict to avoid modifying the original during iteration
        result = {}

        # Check if this is a FileStack-like dict with content
        if "content" in data:
            # Make a copy of the dict without the content field
            result = {
                k: filter_file_content(v) for k, v in data.items() if k != "content"
            }
        else:
            # Process all items recursively
            for key, value in data.items():
                result[key] = filter_file_content(value)

        return result

    elif isinstance(data, list):
        # Process each item in the list recursively
        return [filter_file_content(item) for item in data]

    elif hasattr(data, "__dict__"):
        # Handle object instances by converting to dict and processing
        obj_dict = data.__dict__.copy()
        filtered_dict = filter_file_content(obj_dict)

        # This is tricky - we'd ideally create a new instance with the filtered data
        # but that's complex without knowing the class structure
        # So we'll return the filtered dict instead
        return filtered_dict

    else:
        # For primitive types, return as is
        return data
