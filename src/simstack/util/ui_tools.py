from typing import Dict, Any, List


def ui_hide_fields(ui_schema: Dict[str, Any], fields: str | List[str]) -> dict:
    """
    Hides specified fields in the JSON schema.

    Args:
        ui_schema: The original JSON schema
        fields: List of field names to hide
    """

    local_fields = [fields] if isinstance(fields, str) else fields
    # Iterate through the fields to hide
    for field in local_fields:
        ui_schema[field] = {"ui:widget": "hidden"}
    return ui_schema


def ui_make_properties_optional(
    json_schema: Dict[str, Any], properties: List[str], option_field: str = "Show More"
) -> dict:
    """
    Modifies a JSON schema to make specified properties visible only when an option is enabled.

    Args:
        cls: The class to which the JSON schema belongs
        json_schema: The original JSON schema
        properties: List of property names to make optional
        option_field: Name of the boolean field that controls visibility

    Returns:
        Modified JSON schema with dependencies
    """
    # Add the option field

    if option_field in json_schema["properties"]:
        assert json_schema["properties"][option_field]["type"] == "boolean"
    else:
        json_schema["properties"][option_field] = {
            "type": "boolean",
            "title": option_field,
            "default": False,
        }

    # Create dependencies object if it doesn't exist
    if "dependencies" not in json_schema:
        json_schema["dependencies"] = {}

    new_props = {}
    for prop in properties:
        if prop not in json_schema["properties"]:
            raise ValueError(f"Property {prop} is not in json_schema")
        new_props[prop] = json_schema["properties"][prop]
        del json_schema["properties"][prop]

    # Add dependency rules for each property
    sub_schema = {"properties": {option_field: {"enum": [True]}, **new_props}}
    json_schema["dependencies"][option_field] = {
        "oneOf": [
            {
                "properties": {
                    option_field: {"enum": [False]},
                    **{prop: {"type": "null"} for prop in properties},
                }
            },
            sub_schema,
        ]
    }

    return sub_schema


#
# def ui_make_dependencies_optional(cls, json_schema: Dict[str, Any], option_field_path: List[str],
#                                   properties: List[str], new_option_field: str = "Show More Details") -> Dict[str, Any]:
#     """
#     Makes properties optional within an already optional section at any nesting level.
#
#     Args:
#         cls: The class to which the JSON schema belongs
#         json_schema: The original JSON schema
#         option_field_path: List of option field names representing the path to the target subschema
#         properties: List of property names to make optional within the subschema
#         new_option_field: Name of the new boolean field that controls visibility
#
#     Returns:
#         Modified JSON schema with nested optional properties
#     """
#
#     # Find the target subschema by traversing the path
#     def find_subschema(schema, path, current_depth=0):
#         if current_depth >= len(path):
#             return schema
#
#         current_field = path[current_depth]
#
#         if "dependencies" not in schema or current_field not in schema["dependencies"]:
#             raise ValueError(f"Option field '{current_field}' not found in dependencies at depth {current_depth}")
#
#         dependency = schema["dependencies"][current_field]
#         if "oneOf" not in dependency or len(dependency["oneOf"]) < 2:
#             raise ValueError(f"Invalid dependency structure for '{current_field}' at depth {current_depth}")
#
#         # The subschema is the second element in the oneOf array (index 1)
#         subschema = dependency["oneOf"][1]
#
#         # Continue traversing
#         return find_subschema(subschema, path, current_depth + 1)
#
#     # Find the target subschema
#     target_schema = find_subschema(json_schema, option_field_path)
#
#     # Add the new option field to the target schema
#     target_schema["properties"][new_option_field] = {
#         "type": "boolean",
#         "title": new_option_field,
#         "default": False
#     }
#
#     # Move specified properties to a new dependency within the target schema
#     new_props = {}
#     for prop in properties:
#         if prop not in target_schema["properties"]:
#             raise ValueError(f"Property '{prop}' is not in the target schema")
#         new_props[prop] = target_schema["properties"][prop]
#         del target_schema["properties"][prop]
#
#     # Create dependencies in the target schema if it doesn't exist
#     if "dependencies" not in target_schema:
#         target_schema["dependencies"] = {}
#
#     # Add nested dependency
#     nested_sub_schema = {
#         "properties": {
#             new_option_field: {"enum": [True]},
#             **new_props
#         }
#     }
#
#     target_schema["dependencies"][new_option_field] = {
#         "oneOf": [
#             {
#                 "properties": {
#                     new_option_field: {"enum": [False]},
#                     **{prop: {"type": "null"} for prop in properties}
#                 }
#             },
#             nested_sub_schema
#         ]
#     }
#
#     # The changes are made directly to the target schema which is a reference
#     # to part of json_schema, so we don't need to explicitly update json_schema
#     return json_schema
#


def ui_make_title(cls, ui_schema: Dict[str, Any], field: str, title: str) -> dict:
    """
    Adds a title to the JSON schema.

    Args:
        cls: The class to which the JSON schema belongs
        ui_schema: The original ui_schema schema
        title: Title to be added

    Returns:
        Modified JSON schema with title
    """
    ui_schema[field] = {"ui:title": title}
    return ui_schema


def ui_make_foldable(ui_schema: Dict[str, Any], field: str) -> dict:
    """
    Adds a foldable section to the JSON schema.

    Args:
        field: The field to be made foldable
        ui_schema: The original ui_schema schema
    Returns:
        Modified ui_schema with foldable section

    """
    # TODO add checks that the fields actually exist
    if field not in ui_schema:
        ui_schema[field] = {}
    if "ui:options" not in ui_schema:
        ui_schema[field]["ui:options"] = {}
    ui_schema[field]["ui:options"] = {
        "ui:foldable": "true",
    }
    return ui_schema


def ui_line_vector(ui_schema: Dict[str, Any], field: str) -> Dict[str, Any]:
    """
    Modifies the JSON schema to represent a line vector.

    Args:
        ui_schema: The original JSON schema

    Returns:
        Modified JSON schema with line vector representation
    """
    # TODO add checks that the fields actually exist and is a list
    if field not in ui_schema:
        ui_schema[field] = {}
    if "ui:options" not in ui_schema:
        ui_schema[field]["ui:options"] = {}
    ui_schema[field]["ui:options"]["ui:field"] = "LineVector"
    return ui_schema
