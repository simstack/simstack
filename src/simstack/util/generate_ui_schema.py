from typing import Union


def generate_ui_schema(cls):
    """
    Generates a UI schema that uses GenericForm for fields with ui_schema function.
    Also preserves any existing UI schema configurations from the class.

    Args:
        cls: The model class to generate UI schema for

    Returns:
        dict: The generated UI schema
    """
    # Get base UI schema from the class if it exists

    base_schema = cls.ui_base_schema() if hasattr(cls, "ui_base_schema") else {}

    # Process each field in the model
    for field_name, field in cls.model_fields.items():
        field_type = field.annotation

        # Unwrap Optional type to get the underlying type
        if hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
            # Optional[X] is implemented as Union[X, None]
            args = field_type.__args__
            if len(args) == 2 and args[1] is type(None):
                field_type = args[0]

        # Handle Lists by checking their inner type
        is_list = hasattr(field_type, "__origin__") and field_type.__origin__ is list
        if is_list:
            field_type = field_type.__args__[0]

            # Check for Optional inside List
            if hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
                args = field_type.__args__
                if len(args) == 2 and args[1] is type(None):
                    field_type = args[0]

        # Check if field type has ui_schema method (this is true for all simstack_model classes)
        if hasattr(field_type, "ui_schema"):
            module_path = field_type.__module__
            full_path = f"{module_path}.{field_type.__name__}"
            # if simstack is in the module path, skip all elements after the last simstack occurrence
            if "simstack" in module_path:
                full_path = ".".join(
                    module_path[module_path.rfind("simstack") :].split(".")
                    + [field_type.__name__]
                )

            field_schema = field_type.ui_schema()  # get the field schema for the field
            # Don't override if there's already a UI configuration for this field
            if field_name not in base_schema:
                if "ui:field" in field_schema:
                    # If the field schema already has a ui:field, use it
                    if is_list:
                        base_schema[field_name] = {"items": field_schema}
                    else:
                        base_schema[field_name] = field_schema
                else:
                    # Check if field type is a list
                    ui_ref = {
                        "ui:field": "GenericFormField",
                        "ui:options": {
                            "model": full_path,
                        },
                    }
                    # in rjsf the ui_scheme for list element must be in "items" key
                    if is_list:
                        base_schema[field_name] = {"items": ui_ref}
                    else:
                        ui_ref["ui:options"]["accordion"] = "true"
                        base_schema[field_name] = ui_ref

    # Always hide the id field if it exists
    if "id" not in base_schema:
        base_schema["id"] = {"ui:widget": "hidden"}

    return base_schema
