from functools import wraps
from typing import TypeVar, Type, Any, get_type_hints, overload

from simstack.util.cleaned_json_schema import cleaned_json_schema
from simstack.util.custom_model_dump import custom_model_dump
from simstack.util.default_from_dict import default_from_dict, default_from_model
from simstack.util.generate_ui_schema import generate_ui_schema
from simstack.util.ui_tools import ui_make_title

T = TypeVar("T")


@overload
def simstack_model(cls: Type[T]) -> Type[T]:
    ...


def simstack_model(cls: T) -> T:
    """
    Decorates a given class to equip it with default implementations of utility
    methods for handling operations such as dictionary conversion, schema
    generation, and UI schema generation.
    """

    # Function to create a properly typed wrapper that preserves docstrings
    def create_typed_wrapper(func, first_param_name="this_class"):
        # Get original type hints
        original_hints = get_type_hints(func)

        # Create a wrapper with the correct parameters
        @wraps(func)  # This preserves metadata like docstrings
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        # Add explicit docstring if not preserved by wraps
        if not wrapper.__doc__ and func.__doc__:
            wrapper.__doc__ = func.__doc__

        # Copy the original function's annotations
        wrapper.__annotations__ = {
            first_param_name: Type[Any],  # the first param is now the class
            **{
                k: v
                for k, v in original_hints.items()
                if k != "return" and k != first_param_name
            },
        }

        # Preserve the return annotation if it exists
        if "return" in original_hints:
            wrapper.__annotations__["return"] = original_hints["return"]

        return wrapper

    # Create typed wrappers for all functions with explicit docstrings
    default_class_methods = {
        "json_schema": create_typed_wrapper(cleaned_json_schema),
        "ui_schema": create_typed_wrapper(generate_ui_schema),
        "ui_make_title": create_typed_wrapper(ui_make_title),
        "from_dict": create_typed_wrapper(default_from_dict),
        "from_model": create_typed_wrapper(default_from_model),
        #'make_column_defs': create_typed_wrapper(make_column_defs_helper),
    }

    # Add methods only if they don't exist
    for method_name, default_implementation in default_class_methods.items():
        if not hasattr(cls, method_name):
            setattr(cls, method_name, classmethod(default_implementation))

    default_methods = {
        "custom_model_dump": custom_model_dump,
        #'make_table_entries': make_table_entries_helper
    }

    # Add methods only if they don't exist
    for method_name, default_implementation in default_methods.items():
        if not hasattr(cls, method_name):
            setattr(cls, method_name, default_implementation)

    # Add a marker attribute to identify decorated classes
    setattr(cls, "_is_simstack_model", True)

    # Return the original class without casting - the overload handles the typing
    return cls


def is_simstack_model(cls: Type) -> bool:
    """
    Check if a class has been decorated with @simstack_model.

    Args:
        cls: The class to check

    Returns:
        bool: True if the class was decorated with @simstack_model, False otherwise
    """
    return getattr(cls, "_is_simstack_model", False)
