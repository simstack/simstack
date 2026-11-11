from datetime import datetime
from typing import Dict, Any, Union, List

from simstack.models.document import EmbeddedModel, Field, Model

from simstack.models import simstack_model


def _get_json_schema(data: Dict) -> dict:
    """
    Inspects the current data dict and returns a JSON schema for that dict.

    Returns:
        dict: JSON schema describing the structure and types of the current data dict
    """
    if not data:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    properties = {}

    for key, value in data.items():
        if isinstance(value, str):
            properties[key] = {"type": "string"}
        elif isinstance(value, bool):
            # Check bool before int/float since bool is a subclass of int in Python
            properties[key] = {"type": "boolean"}
        elif isinstance(value, int):
            properties[key] = {"type": "integer"}
        elif isinstance(value, float):
            properties[key] = {"type": "number"}
        elif isinstance(value, datetime):
            properties[key] = {"type": "string", "format": "date-time"}
        else:
            # Fallback for any unexpected types
            properties[key] = {"type": "string"}

    schema = {"type": "object", "properties": properties, "additionalProperties": False}

    return schema


class DataSetMetadataTemplate(Model):
    dataset_type: str
    model_json: dict[str, Any]
    structure: Dict[str, Dict[str, str]] = Field(default_factory=dict)


@simstack_model
class DataSetMetadata(EmbeddedModel):
    field_name: str = Field(unique=True)
    data: Dict[str, Union[str, int, float, bool, datetime]] = Field(
        default_factory=dict
    )
    is_validated: bool = False
    structure: Dict[str, Dict[str, str]] = Field(default_factory=dict)

    def get_json_schema(self):
        return _get_json_schema(self.data)

    @property
    def dataset_type(self) -> str:
        return self.field_name

    @property
    def initialized(self) -> bool:
        """Structural edits are locked only after explicit validation/freeze."""
        return self.is_validated

    # Dict-like behavior methods
    def __getitem__(self, key: str):
        """Get item from data dict."""
        return self.data[key]

    def __setitem__(self, key: str, value: Union[str, int, float, bool, datetime]):
        """Set item in data dict with validation."""
        # Validate value type
        if not isinstance(value, (str, int, float, bool, datetime)):
            raise TypeError(
                f"Value must be str, int, float, bool, or datetime, got {type(value).__name__}"
            )

        # Only check for structural changes after initialization
        if self.initialized and key not in self.data:
            raise KeyError(
                f"Cannot add new key '{key}' after initialization. Existing keys: {list(self.data.keys())}"
            )

        # Only check for type changes after initialization
        if self.initialized and key in self.data:
            existing_value = self.data[key]
            if type(existing_value) != type(value):
                raise TypeError(
                    f"Cannot change type of key '{key}' from {type(existing_value).__name__} "
                    f"to {type(value).__name__}"
                )

        self.data[key] = value

    def __delitem__(self, key: str):
        """Delete item from data dict."""
        if self.initialized:
            raise KeyError(f"Cannot delete key '{key}' after initialization")
        del self.data[key]

    def __contains__(self, key: str) -> bool:
        """Check if key exists in data dict."""
        return key in self.data

    def __iter__(self):
        """Iterate over keys in data dict."""
        return iter(self.data)

    def __len__(self) -> int:
        """Get number of items in data dict."""
        return len(self.data)

    def keys(self):
        """Get keys from data dict."""
        return self.data.keys()

    def values(self):
        """Get values from data dict."""
        return self.data.values()

    def items(self):
        """Get items from data dict."""
        return self.data.items()

    def get(self, key: str, default=None):
        """Get item from data dict with default."""
        return self.data.get(key, default)

    def pop(self, key: str, *args):
        """Pop item from data dict."""
        if self.initialized:
            raise KeyError(f"Cannot pop key '{key}' after initialization")
        return self.data.pop(key, *args)

    def popitem(self):
        """Pop item from data dict."""
        if self.initialized:
            raise KeyError("Cannot pop items after initialization")
        return self.data.popitem()

    def clear(self):
        """Clear data dict."""
        if self.initialized:
            raise RuntimeError("Cannot clear data after initialization")
        self.data.clear()

    def update(self, *args, **kwargs):
        """Update data dict with validation."""
        # Handle different update signatures
        if args:
            other = args[0]
            if hasattr(other, "items"):
                items_to_update = other.items()
            else:
                items_to_update = other
        else:
            items_to_update = []

        # Combine with kwargs
        all_items = list(items_to_update) + list(kwargs.items())

        # Validate all items before updating
        for key, value in all_items:
            # Type validation (always required)
            if not isinstance(value, (str, int, float, bool, datetime)):
                raise TypeError(
                    f"Value for key '{key}' must be str, int, float, bool, or datetime, got {type(value).__name__}"
                )

            # Structural validation (only after initialization)
            if self.initialized:
                if key not in self.data:
                    raise KeyError(f"Cannot add new key '{key}' after initialization")

                existing_value = self.data[key]
                if type(existing_value) != type(value):
                    raise TypeError(
                        f"Cannot change type of key '{key}' from {type(existing_value).__name__} "
                        f"to {type(value).__name__}"
                    )

        # If all validations pass, update the data
        for key, value in all_items:
            self.data[key] = value

    def setdefault(self, key: str, default=None):
        """Set default value for key if not exists."""
        if key not in self.data:
            if self.initialized:
                raise KeyError(f"Cannot add new key '{key}' after initialization")

            if default is not None and not isinstance(
                default, (str, int, float, bool, datetime)
            ):
                raise TypeError(
                    f"Default value must be str, int, float, bool, or datetime, got {type(default).__name__}"
                )

            self.data[key] = default

        return self.data[key]

    # Additional utility methods (no initialization checks needed)
    def copy_data(self) -> Dict[str, Union[str, int, float, bool, datetime]]:
        """Return a copy of the data dict."""
        return self.data.copy()

    def is_type_compatible(self, key: str, value) -> bool:
        """Check if a value is type-compatible with existing key."""
        if key not in self.data:
            return isinstance(value, (str, int, float, bool, datetime))
        return type(self.data[key]) == type(value)

    def get_key_type(self, key: str) -> type:
        """Get the type of a specific key."""
        if key not in self.data:
            raise KeyError(f"Key '{key}' not found")
        return type(self.data[key])

    def get_schema_for_key(self, key: str) -> dict:
        """Get JSON schema for a specific key."""
        if key not in self.data:
            raise KeyError(f"Key '{key}' not found")

        value = self.data[key]
        if isinstance(value, str):
            return {"type": "string"}
        elif isinstance(value, bool):
            return {"type": "boolean"}
        elif isinstance(value, int):
            return {"type": "integer"}
        elif isinstance(value, float):
            return {"type": "number"}
        elif isinstance(value, datetime):
            return {"type": "string", "format": "date-time"}
        else:
            return {"type": "string"}


def validate_metadata_template(metadata: DataSetMetadata, reference: DataSetMetadataTemplate | None,
                               structure, *, dataset_tuple=False, freeze=False):
    """Pure metadata validation; the Server owns loading/persisting its template."""
    from copy import deepcopy
    structure = {name: value for name, value in structure.items() if value}
    if freeze:
        if reference is None:
            raise ValueError("Metadata does not exist")
        if metadata.structure != reference.structure:
            raise ValueError("Metadata structure has changed")
        updated = reference.model_copy(deep=True)
        if not updated.structure:
            updated.structure = structure
        return updated, structure == updated.structure
    if reference is None:
        return DataSetMetadataTemplate(dataset_type=metadata.field_name,
            model_json=_get_json_schema(metadata.data), structure=structure), True
    reference_properties = reference.model_json.get("properties", {})
    properties = _get_json_schema(metadata.data).get("properties", {})
    if set(reference_properties) != set(properties):
        raise ValueError("Data schema properties mismatch")
    for key, expected in reference_properties.items():
        actual = properties[key]
        if expected.get("type") != actual.get("type"):
            raise ValueError(f"Property '{key}' type mismatch")
        ignored = {"format"} if expected.get("type") == "string" else set()
        if {k: v for k, v in expected.items() if k not in ignored} != {
            k: v for k, v in actual.items() if k not in ignored
        }:
            raise ValueError(f"Property '{key}' schema mismatch")
    updated_structure = deepcopy(reference.structure)
    for section_name, section in structure.items():
        expected = updated_structure.get(section_name)
        if expected is None:
            updated_structure[section_name] = dict(section)
        elif dataset_tuple and expected != section:
            raise ValueError(f"Section '{section_name}' content mismatch")
        else:
            for key, value in section.items():
                if key in expected and expected[key] != value:
                    raise ValueError(f"Section '{section_name}' has different value for key '{key}'")
                expected[key] = value
    updated = reference.model_copy(deep=True)
    updated.structure = updated_structure
    return updated, True
