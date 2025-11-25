from datetime import datetime
from typing import Union, Dict, Any


def make_table_entries_helper(
    model_instance,
    table_name=None,
    max_recursion_level=1,
    drop_id=True,
    current_level=0,
    visited=None,
    field_prefix="",
):
    """
    Create table data for AG Grid from a model instance.
    Handles datetime objects properly and can optionally drop ID fields.

    Args:
        model_instance: The model instance to process
        table_name: Optional name for the table (default: None, uses class name)
        max_recursion_level: Maximum depth for processing nested models (default: 1)
        drop_id: Whether to drop the ID field (default: True)
        current_level: Current recursion level (default: 0)
        visited: Set of objects already visited to prevent infinite recursion (default: None)
        field_prefix: Prefix for field paths in nested structures (default: "")

    Returns:
        dict: Dictionary with 'tableName' and 'tableData' (rows data for AG Grid)
    """
    # Initialize the visited set to track objects for preventing infinite recursion
    if visited is None:
        visited = set()

    # If the instance has already been visited, stop recursion
    if id(model_instance) in visited:
        return {"tableData": {}}

    # Mark this instance as visited
    visited.add(id(model_instance))

    # Get model class and fields
    model_class = type(model_instance)

    if hasattr(model_instance, "make_table_entries"):
        field_prefix = f"{field_prefix}." if field_prefix else ""
        field_prefix = (
            f"{field_prefix}{model_instance.__class__.__name__}"
            if field_prefix
            else model_instance.__class__.__name__
        )
        return model_instance.make_table_entries(
            max_recursion_level=1,
            drop_id=True,
            current_level=0,
            visited=None,
            field_prefix="",
        )

    model_fields = getattr(model_instance, "model_fields", {})

    # Use class name if table_name not provided
    if table_name is None:
        table_name = model_class.__name__

    # Initialize empty summary dictionary
    summary = {}

    # Process each field
    for field_name, field_type in model_fields.items():
        # Skip ID fields if drop_id is True
        if drop_id and (field_name == "id" or field_name == "_id"):
            continue

        # Calculate the full field path for AG Grid
        field_path = f"{field_prefix}.{field_name}" if field_prefix else field_name

        # Get field value
        field_value = getattr(model_instance, field_name, None)

        # If the field value is None, add it to summary
        if field_value is None:
            summary[field_name] = None
            continue

        # Handle datetime objects for AG Grid
        if isinstance(field_value, datetime):
            summary[field_name] = field_value.isoformat()
            continue

        # Check if the field is a nested model
        is_nested_model = hasattr(field_value, "model_fields")

        # If the recursion level is 0, exclude all nested objects
        if max_recursion_level == 0 and is_nested_model:
            continue

        # Handle nested model if it exists and we haven't reached max recursion
        if is_nested_model and current_level < max_recursion_level:
            # Check if the object has its own make_table method
            if hasattr(field_value, "make_table_entries"):
                nested_result = field_value.make_table_entries(
                    max_recursion_level=max_recursion_level,
                    current_level=current_level + 1,
                    visited=visited,
                    field_prefix=f"{field_path}.",
                )

                if isinstance(nested_result, dict) and "tableData" in nested_result:
                    nested_summary = nested_result["tableData"]
                else:
                    # If make_table doesn't return expected format
                    nested_summary = nested_result
            else:
                # Process the nested model recursively
                nested_result = make_table_entries_helper(
                    field_value,
                    f"{table_name}_{field_name}",
                    max_recursion_level,
                    drop_id,
                    current_level + 1,
                    visited,
                    f"{field_path}.",
                )
                nested_summary = nested_result

            # Add the nested object as a sub-dictionary
            summary[field_name] = nested_summary

        elif field_type == "self" and current_level < max_recursion_level:
            # Handle self-referential field, but only go one level deep to avoid cycles
            raise ValueError(
                f"Self-referential field '{field_name}' is not supported in make_table_entries."
            )
            if field_value is not None and id(field_value) not in visited:
                # Process only the direct fields of the self-reference
                nested_result = make_table_entries_helper(
                    field_value,
                    f"{table_name}_{field_name}",
                    0,  # Only process direct fields, no further recursion
                    drop_id,
                    current_level + 1,
                    visited,
                    f"{field_path}.",
                )
                nested_summary = nested_result["tableData"]
                summary[field_name] = nested_summary
        else:
            # Handle lists/arrays
            if isinstance(field_value, list):
                processed_list = []
                for item in field_value:
                    if isinstance(item, datetime):
                        processed_list.append(item.isoformat())
                    elif (
                        hasattr(item, "model_fields")
                        and current_level < max_recursion_level
                    ):
                        # Handle nested models in lists
                        if hasattr(item, "make_table"):
                            nested_result = item.make_table_entries(
                                max_recursion_level=0,
                                current_level=current_level + 1,
                                visited=visited.copy(),
                            )

                            if (
                                isinstance(nested_result, dict)
                                and "tableData" in nested_result
                            ):
                                processed_list.append(nested_result["tableData"])
                            else:
                                processed_list.append(nested_result)
                        else:
                            nested_result = make_table_entries_helper(
                                item,
                                f"{table_name}_{field_name}_item",
                                0,
                                drop_id,
                                current_level + 1,
                                visited.copy(),
                                "",
                            )
                            processed_list.append(nested_result["tableData"])
                    else:
                        processed_list.append(item)
                summary[field_name] = processed_list
            else:
                # This is a simple field, add it directly
                summary[field_name] = field_value

    return summary


def is_pydantic_model(obj):
    """
    Check if an object is a Pydantic model class.

    Args:
        obj: The object to check

    Returns:
        bool: True if the object is a Pydantic model class, False otherwise
    """
    # For Pydantic V1
    if hasattr(obj, "__fields__"):
        return True

    # For Pydantic V2
    if hasattr(obj, "model_fields"):
        return True

    # Check for BaseModel inheritance (works for both V1 and V2)
    if isinstance(obj, type):
        from inspect import getmro

        for base in getmro(obj):
            if base.__name__ == "BaseModel":
                return True

    # Handle typing objects like Optional[Model]
    if hasattr(obj, "__origin__") and hasattr(obj, "__args__"):
        # Check if it's a Union type (which includes Optional)
        from typing import _SpecialGenericAlias, _GenericAlias

        if (
            isinstance(obj, (_SpecialGenericAlias, _GenericAlias))
            and obj.__origin__ is Union
        ):
            return any(is_pydantic_model(arg) for arg in obj.__args__)

    return False


def make_column_defs_helper(
    model_class,
    table_name=None,
    max_recursion_level=1,
    drop_id=True,
    current_level=0,
    visited=None,
    field_prefix="",
):
    """
    Create column definitions for AG Grid based on a model class.
    Uses the same logic as table data generation but for model class.
    This is a helper function mapped to make_column_defs in simstack_model.
    The idea is that most classes can call the helper function but some classes might
    want to override it to return specific column definitions.

    Args:
        model_class: The model class to process
        table_name: Optional name for the table (default: None, uses class name)
        max_recursion_level: Maximum depth for processing nested models (default: 1)
        drop_id: Whether to drop the ID field (default: True)
        current_level: Current recursion level (default: 0)
        visited: Set of objects already visited to prevent infinite recursion (default: None)
        field_prefix: Prefix for field paths in nested structures (default: "")

    Returns:
        list: Column definitions for AG Grid
    """
    from typing import get_origin, get_args, List, Union

    # Initialize a visited set to track classes for preventing infinite recursion
    if visited is None:
        visited = set()

    # If the class has already been visited, stop recursion
    if id(model_class) in visited:
        return []

    # Mark this class as visited
    visited.add(id(model_class))

    if hasattr(model_class, "make_column_defs"):
        return model_class.make_column_defs(
            model_class,
            table_name=None,
            max_recursion_level=1,
            drop_id=True,
            current_level=0,
            visited=None,
            field_prefix="",
        )
    # Get model fields
    model_fields = getattr(
        model_class, "model_fields", getattr(model_class, "__fields__", {})
    )

    # Use class name if table_name not provided
    if table_name is None:
        table_name = model_class.__name__

    # Initialize column definitions
    column_defs = []

    # Process each field
    for field_name, field_info in model_fields.items():
        # Skip ID fields if drop_id is True
        if drop_id and (field_name == "id" or field_name == "_id"):
            continue

        # Calculate the full field path for AG Grid
        field_path = f"{field_prefix}{field_name}" if field_prefix else field_name

        # Extract field type (different in V1 and V2)
        if hasattr(field_info, "annotation"):
            # Pydantic V2
            field_type = field_info.annotation
        elif hasattr(field_info, "type_"):
            # Pydantic V1
            field_type = field_info.type_
        else:
            # Fallback
            field_type = None

        # Check if the field type is a Pydantic model
        is_nested_model = is_pydantic_model(field_type)

        # Extract actual model class for nested types (like Optional[Model])
        if is_nested_model and hasattr(field_type, "__args__"):
            args = get_args(field_type)
            for arg in args:
                if is_pydantic_model(arg):
                    field_type = arg
                    break

        # Create column definition based on the field type
        column_def = create_column_def(field_name, field_type, None, field_path)

        # Set object type for nested models
        if is_nested_model:
            column_def["type"] = "objectColumn"

        # If recursion level is 0, exclude all nested objects but include column def for the field itself
        if max_recursion_level == 0 and is_nested_model:
            column_defs.append(column_def)
            continue

        # Handle nested model class if it exists and we haven't reached max recursion
        if is_nested_model and current_level < max_recursion_level:
            # Check if the class has its own make_columns method
            if hasattr(field_type, "make_column_defs"):
                nested_columns = field_type.make_column_defs(
                    max_recursion_level=max_recursion_level,
                    current_level=current_level + 1,
                    visited=visited.copy(),
                )
            else:
                # Process the nested model class recursively
                nested_columns = make_column_defs_helper(
                    field_type,
                    f"{table_name}_{field_name}",
                    max_recursion_level,
                    drop_id,
                    current_level + 1,
                    visited.copy(),
                    f"{field_path}.",
                )

            # Create a column group for nested fields if we have columns
            if nested_columns:
                group_column = {
                    "headerName": field_name.capitalize(),
                    "marryChildren": True,
                    "children": nested_columns,
                }
                column_defs.append(group_column)
            else:
                column_defs.append(column_def)
        elif field_type == "self" and current_level < max_recursion_level:
            # Handle self-referential field type
            # Create a new visited set for this branch to avoid affecting other branches
            self_visited = visited.copy()
            if model_class not in self_visited:
                # Process only the direct fields of the self-reference
                nested_columns = make_column_defs_helper(
                    model_class,
                    f"{table_name}_{field_name}",
                    0,  # Only process direct fields, no further recursion
                    drop_id,
                    current_level + 1,
                    self_visited,
                    f"{field_path}.",
                )

                # Create a column group for self-referential fields
                if nested_columns:
                    group_column = {
                        "headerName": field_name.capitalize(),
                        "marryChildren": True,
                        "children": nested_columns,
                    }
                    column_defs.append(group_column)
                else:
                    column_defs.append(column_def)
            else:
                column_defs.append(column_def)
        else:
            # Handle lists/arrays
            is_list_type = False
            inner_type = None

            # Check for various list types using get_origin and get_args
            origin = get_origin(field_type)
            if origin is list or origin is List:
                is_list_type = True
                args = get_args(field_type)
                if args:
                    inner_type = args[0]
            # Handle Optional[List[...]]
            elif origin is Union:
                args = get_args(field_type)
                for arg in args:
                    arg_origin = get_origin(arg)
                    if arg_origin is list or arg_origin is List:
                        is_list_type = True
                        arg_args = get_args(arg)
                        if arg_args:
                            inner_type = arg_args[0]
                        break

            if is_list_type:
                # For lists, we add a special column definition
                list_column_def = {
                    "headerName": field_name.capitalize(),
                    "field": field_path,
                    "cellRenderer": "agArrayCellRenderer",
                }

                # If the list contains models, we might want to indicate that
                if inner_type and is_pydantic_model(inner_type):
                    list_column_def["type"] = "modelArrayColumn"

                column_defs.append(list_column_def)
            else:
                # This is a simple field, add column definition directly
                column_defs.append(column_def)

    return column_defs


def make_column_defs_instance(
    model_instance,
    table_name=None,
    max_recursion_level=1,
    drop_id=True,
    current_level=0,
    visited=None,
    field_prefix="",
):
    """
    Create column definitions for AG Grid based on a model instance.
    Uses the same logic as table data generation but works on an actual instance.
    This allows for dynamic column generation based on actual data values.

    Args:
        model_instance: The model instance to process
        table_name: Optional name for the table (default: None, uses class name)
        max_recursion_level: Maximum depth for processing nested models (default: 1)
        drop_id: Whether to drop the ID field (default: True)
        current_level: Current recursion level (default: 0)
        visited: Set of objects already visited to prevent infinite recursion (default: None)
        field_prefix: Prefix for field paths in nested structures (default: "")

    Returns:
        list: Column definitions for AG Grid
    """
    from typing import get_origin, get_args, List, Union

    # Initialize a visited set to track instances for preventing infinite recursion
    if visited is None:
        visited = set()

    # If the instance has already been visited, stop recursion
    instance_id = id(model_instance)
    if instance_id in visited:
        return []

    # Mark this instance as visited
    visited.add(instance_id)

    # Check if the instance has a custom make_column_defs method
    if hasattr(model_instance, "make_column_defs_instance") and callable(
        getattr(model_instance, "make_column_defs_instance")
    ):
        return model_instance.make_column_defs_instance(
            table_name=table_name,
            max_recursion_level=max_recursion_level,
            drop_id=drop_id,
            current_level=current_level,
            visited=visited.copy(),
            field_prefix=field_prefix,
        )

    # Get model class and fields
    model_class = type(model_instance)
    model_fields = getattr(
        model_class, "model_fields", getattr(model_class, "__fields__", {})
    )

    # Use class name if table_name not provided
    if table_name is None:
        table_name = model_class.__name__

    # Initialize column definitions
    column_defs = []

    # Process each field
    for field_name, field_info in model_fields.items():
        # Skip ID fields if drop_id is True
        if drop_id and (field_name == "id" or field_name == "_id"):
            continue

        # Calculate the full field path for AG Grid
        field_path = f"{field_prefix}.{field_name}" if field_prefix else field_name

        # Get the actual field value from the instance
        try:
            field_value = getattr(model_instance, field_name, None)
        except AttributeError:
            field_value = None

        # Extract field type (different in V1 and V2)
        if hasattr(field_info, "annotation"):
            # Pydantic V2
            field_type = field_info.annotation
        elif hasattr(field_info, "type_"):
            # Pydantic V1
            field_type = field_info.type_
        else:
            # Fallback - infer from actual value
            field_type = type(field_value) if field_value is not None else str

        # Check if the field type is a Pydantic model
        is_nested_model = is_pydantic_model(field_type)

        # Extract actual model class for nested types (like Optional[Model])
        if is_nested_model and hasattr(field_type, "__args__"):
            args = get_args(field_type)
            for arg in args:
                if is_pydantic_model(arg):
                    field_type = arg
                    break

        # For instances, we can also infer type from actual value
        if field_value is not None and is_pydantic_model(type(field_value)):
            field_type = type(field_value)
            is_nested_model = True

        # Create column definition based on the field type and actual value
        column_def = create_column_def(field_name, field_type, field_value, field_path)

        # Set object type for nested models
        if is_nested_model:
            column_def["type"] = "objectColumn"

        # If recursion level is 0, exclude all nested objects but include column def for the field itself
        if max_recursion_level == 0 and is_nested_model:
            column_defs.append(column_def)
            continue

        # Handle nested model instance if it exists and we haven't reached max recursion
        if (
            is_nested_model
            and field_value is not None
            and current_level < max_recursion_level
        ):
            # Check if the instance has its own make_column_defs method
            if hasattr(field_value, "make_column_defs_instance") and callable(
                getattr(field_value, "make_column_defs_instance")
            ):
                nested_columns = field_value.make_column_defs_instance(
                    max_recursion_level=max_recursion_level,
                    current_level=current_level + 1,
                    visited=visited.copy(),
                    field_prefix=f"{field_path}." if field_path else "",
                )
            else:
                # Process the nested model instance recursively
                nested_columns = make_column_defs_instance(
                    field_value,
                    f"{table_name}_{field_name}",
                    max_recursion_level,
                    drop_id,
                    current_level + 1,
                    visited.copy(),
                    f"{field_path}.",
                )

            # Create a column group for nested fields if we have columns
            if nested_columns:
                group_column = {
                    "headerName": field_name.capitalize(),
                    "marryChildren": True,
                    "children": nested_columns,
                }
                column_defs.append(group_column)
            else:
                column_defs.append(column_def)
        elif field_type == "self" and current_level < max_recursion_level:
            # Handle self-referential field type
            if field_value is not None:
                # Create a new visited set for this branch to avoid affecting other branches
                self_visited = visited.copy()
                if instance_id not in self_visited:
                    # Process only the direct fields of the self-reference
                    nested_columns = make_column_defs_instance(
                        field_value,
                        f"{table_name}_{field_name}",
                        0,  # Only process direct fields, no further recursion
                        drop_id,
                        current_level + 1,
                        self_visited,
                        f"{field_path}.",
                    )

                    # Create a column group for self-referential fields
                    if nested_columns:
                        group_column = {
                            "headerName": field_name.capitalize(),
                            "marryChildren": True,
                            "children": nested_columns,
                        }
                        column_defs.append(group_column)
                    else:
                        column_defs.append(column_def)
                else:
                    column_defs.append(column_def)
            else:
                column_defs.append(column_def)
        else:
            # Handle lists/arrays
            is_list_type = False
            inner_type = None

            # Check for various list types using get_origin and get_args
            origin = get_origin(field_type)
            if origin is list or origin is List:
                is_list_type = True
                args = get_args(field_type)
                if args:
                    inner_type = args[0]
            # Handle Optional[List[...]]
            elif origin is Union:
                args = get_args(field_type)
                for arg in args:
                    arg_origin = get_origin(arg)
                    if arg_origin is list or arg_origin is List:
                        is_list_type = True
                        arg_args = get_args(arg)
                        if arg_args:
                            inner_type = arg_args[0]
                        break

            # Also check if the actual field value is a list
            if field_value is not None and isinstance(field_value, list):
                is_list_type = True
                # Try to infer inner type from first non-None element
                if field_value and inner_type is None:
                    for item in field_value:
                        if item is not None:
                            inner_type = type(item)
                            break

            if is_list_type:
                # For lists, we add a special column definition
                list_column_def = {
                    "headerName": field_name.capitalize(),
                    "field": field_path,
                    "cellRenderer": "agArrayCellRenderer",
                }

                # If the list contains models, we might want to indicate that
                if inner_type and is_pydantic_model(inner_type):
                    list_column_def["type"] = "modelArrayColumn"

                # If we have actual list data, we can provide more specific info
                if field_value is not None and isinstance(field_value, list):
                    list_column_def["listLength"] = len(field_value)
                    if field_value:
                        # Add info about the first item for reference
                        first_item = field_value[0]
                        if is_pydantic_model(type(first_item)):
                            list_column_def["itemType"] = type(first_item).__name__

                column_defs.append(list_column_def)
            else:
                # This is a simple field, add column definition directly
                column_defs.append(column_def)

    return column_defs


def make_table_entries(
    model_instances, table_name=None, max_recursion_level=1, drop_id=True
):
    """
    Create AG Grid table configuration for a list of model instances.
    This is the main function to make tables that are visible in the ui.

    To generate a table in the ui the ui_schema of the class should initialize
    the grid_options for the table.

    It is basically impossible to predict what a table should be for a class but
    tables can be created only from lists. The make_table function of the class should
    therefore call this function to generate the table for appropriate list fields and
    merge these manually where it makes sense.

    This function calls the helper functions make_table_entries_helper and make_column_defs_helper
    which recursively process the model instances and their fields to generate the table data and column definitions.

    for model fields these functions check if the nested model has functions make_columns or make_column_defs
    which are called to generate the column data and definitions, respectively

    These functions do not exist by default but can be used by models to modify the default definitions
    where they dont work. This is typically the case if the models contain huge data sets that
    we do not want to display in the table. An example of this is in the Molecule class.


    Args:
        model_instances: List of model instances to process
        table_name: Name for the table (default: None, uses class name)
        max_recursion_level: Maximum depth for processing nested models (default: 1)
        drop_id: Whether to drop the ID field (default: True)

    Returns:
        dict: Dictionary with 'tableName', 'rowData' (for AG Grid), and 'columnDefs'
    """
    if not model_instances:
        return {
            "tableName": table_name or "EmptyTable",
            "rowData": [],
            "columnDefs": [],
        }

    # Get the first instance and its class

    first_instance = model_instances[0]
    model_class = type(first_instance)

    # Use the new helper functions
    # table_data_result = make_table_entries_helper(
    #     first_instance, table_name, max_recursion_level, drop_id
    # )

    # Get column definitions using the model class
    column_defs = make_column_defs_helper(
        model_class, table_name, max_recursion_level, drop_id
    )

    # Process all instances to get row data
    row_data = []
    for instance in model_instances:
        instance_result = make_table_entries_helper(
            instance, table_name, max_recursion_level, drop_id
        )
        row_data.append(instance_result)

    return {"tableName": "Table", "rowData": row_data, "columnDefs": column_defs}


def create_column_def(field_name, field_type, field_value, field_path):
    """
    Create a column definition for AG Grid based on field type and value.

    Args:
        field_name: Name of the field
        field_type: Type of the field from model definition
        field_value: Actual value of the field (can be None for class-based definitions)
        field_path: Full path to the field (for nested structures)

    Returns:
        dict: Column definition for AG Grid
    """
    column_def = {
        "headerName": field_name.capitalize(),
        "field": field_path,
    }

    # Determine the column type based on field_value if available or field_type
    if field_value is not None:
        if isinstance(field_value, bool):
            column_def["type"] = "booleanColumn"
        elif isinstance(field_value, (int, float)):
            column_def["type"] = "numericColumn"
        elif isinstance(field_value, datetime):
            column_def["type"] = "dateColumn"
        elif isinstance(field_value, dict):
            column_def["type"] = "objectColumn"
        elif isinstance(field_value, list):
            column_def["type"] = "arrayColumn"
        else:
            column_def["type"] = "textColumn"
    else:
        # Determine type from field_type if field_value is None
        if field_type is bool or (
            isinstance(field_type, type) and issubclass(field_type, bool)
        ):
            column_def["type"] = "booleanColumn"
        elif field_type in (int, float) or (
            isinstance(field_type, type) and issubclass(field_type, (int, float))
        ):
            column_def["type"] = "numericColumn"
        elif hasattr(field_type, "__name__") and field_type.__name__ == "datetime":
            column_def["type"] = "dateColumn"
        else:
            column_def["type"] = "textColumn"

    return column_def


def ui_add_table(ui_options: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add table options to the UI schema for AG Grid.

    the class must then implement o make_table function calling make_table
    :param ui_options:
    :return: modified ui_options
    """
    ui_options["ui:table"] = {  # this goes into gridOptions
        "pagination": True,
        "paginationPageSize": 20,
        "domLayout": "autoHeight",
        "defaultColDef": {"flex": 1, "minWidth": 80, "resizable": True},
    }
    return ui_options  # for chaining
