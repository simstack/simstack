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
    #
    # def _ensure_field_name_model_field(original_cls):
    #     #
    #     # raw_namespace: Dict[str, Any] = dict(original_cls.__dict__)
    #     #
    #     # keep_dunders = {"__module__", "__doc__", "__qualname__", "__annotations__"}
    #     # namespace: Dict[str, Any] = {
    #     #     k: v
    #     #     for k, v in raw_namespace.items()
    #     #     if (k in keep_dunders) or (not k.startswith("_"))
    #     # }
    #
    #     namespace = dict(original_cls.__dict__)
    #     if "_abc_impl" in namespace:
    #         del namespace["_abc_impl"]
    #
    #     # --- ODMantic compatibility ---
    #     # ODMantic enforces validate_default=True and raises if it is changed.
    #     model_config = namespace.get("model_config")
    #     if model_config is not None:
    #         # model_config can be a dict-like config; normalize to a mutable dict
    #         try:
    #             cfg: dict[str, Any] = dict(model_config)
    #         except TypeError:
    #             cfg = {"_raw_model_config": model_config}
    #
    #         if cfg.get("validate_default") is not None:
    #             del cfg["validate_default"]
    #
    #         if cfg.get("validate_assignment") is not None:
    #             del cfg["validate_assignment"]
    #
    #         namespace["model_config"] = cfg
    #     # --- end ODMantic compatibility ---
    #
    #     # Ensure we don't accidentally reuse internals that should be recomputed.
    #     # (We keep __module__/__doc__ and all user-defined attrs.)
    #     namespace.pop("__dict__", None)
    #     namespace.pop("__weakref__", None)
    #
    #
    #     NewCls = type(original_cls.__name__, original_cls.__bases__, namespace)
    #     return NewCls
    #
    # cls = _ensure_field_name_model_field(cls)

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
