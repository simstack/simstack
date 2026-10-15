"""Pure reconciliation rules for dataset metadata templates."""

from __future__ import annotations

import copy
from typing import Dict

from simstack.models.dataset_metadata import (
    DataSetMetadata,
    DataSetMetadataTemplate,
    _get_json_schema,
)


def reconcile_dataset_metadata(
    metadata: DataSetMetadata,
    new_structure: Dict[str, Dict[str, str]],
    template: DataSetMetadataTemplate | None,
    *,
    dataset_tuple: bool = False,
) -> DataSetMetadataTemplate:
    """Validate ``metadata`` and return the explicit template to persist."""

    normalized_structure = {
        key: copy.deepcopy(value)
        for key, value in new_structure.items()
        if value
    }
    if template is None:
        template = DataSetMetadataTemplate(
            dataset_type=metadata.field_name,
            model_json=_get_json_schema(metadata.data),
            structure=normalized_structure,
        )
        metadata.structure = copy.deepcopy(normalized_structure)
        return template

    new_data_json = _get_json_schema(metadata.data)
    reference_properties = template.model_json.get("properties", {})
    new_properties = new_data_json.get("properties", {})
    if set(reference_properties) != set(new_properties):
        raise ValueError(
            "Data schema properties mismatch. "
            f"Reference keys: {set(reference_properties)}, "
            f"Current keys: {set(new_properties)}"
        )

    for key, reference_property in reference_properties.items():
        new_property = new_properties[key]
        reference_type = reference_property.get("type")
        new_type = new_property.get("type")
        if reference_type != new_type:
            raise ValueError(
                f"Property '{key}' type mismatch. "
                f"Reference: {reference_type}, Current: {new_type}"
            )
        if reference_type == "string":
            reference_shape = {
                name: value
                for name, value in reference_property.items()
                if name != "format"
            }
            new_shape = {
                name: value
                for name, value in new_property.items()
                if name != "format"
            }
            if reference_shape != new_shape:
                raise ValueError(
                    f"Property '{key}' schema mismatch (excluding format). "
                    f"Reference: {reference_shape}, Current: {new_shape}"
                )
        elif reference_property != new_property:
            raise ValueError(
                f"Property '{key}' schema mismatch. "
                f"Reference: {reference_property}, Current: {new_property}"
            )

    updated_structure = copy.deepcopy(template.structure)
    for section_name, new_section in normalized_structure.items():
        if section_name not in updated_structure:
            updated_structure[section_name] = copy.deepcopy(new_section)
            continue

        reference_section = updated_structure[section_name]
        if dataset_tuple:
            if reference_section != new_section:
                raise ValueError(
                    f"Section '{section_name}' content mismatch. "
                    f"Reference: {reference_section}, Current: {new_section}"
                )
            continue

        common_keys = set(reference_section).intersection(new_section)
        for key in common_keys:
            if reference_section[key] != new_section[key]:
                raise ValueError(
                    "MetaData Validation failed: "
                    f"Section '{section_name}' has different value for key '{key}'.\n"
                    f"Reference: {reference_section[key]}, Current: {new_section[key]}\n"
                    f"Reference keys: {set(reference_section)}\n"
                    f"Current keys: {set(new_section)}"
                )
        for key in set(new_section) - set(reference_section):
            reference_section[key] = new_section[key]

    template.structure = updated_structure
    metadata.structure = copy.deepcopy(updated_structure)
    return template


def freeze_dataset_metadata(
    metadata: DataSetMetadata,
    new_structure: Dict[str, Dict[str, str]],
    template: DataSetMetadataTemplate,
) -> bool:
    """Apply the pure freeze rule against an explicitly supplied template."""

    if metadata.structure != template.structure:
        raise ValueError("Metadata structure has changed in persistence")
    if metadata.structure == {}:
        metadata.structure = copy.deepcopy(new_structure)
        template.structure = copy.deepcopy(new_structure)
    return new_structure == metadata.structure
