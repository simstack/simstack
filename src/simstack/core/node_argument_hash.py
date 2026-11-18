"""Canonical argument projections used in persisted node cache keys."""

from typing import Any, List, cast

from pydantic import BaseModel

from simstack.core.hash import complex_hash_function
from simstack.models.document import Model
from simstack.models.files import FileStack


class _HashableModel(dict[str, Any]):
    """A typed model projection, distinct from an ordinary scientific mapping."""

    # ComplexHash includes the qualified type name in persisted task cache keys.
    __module__ = "simstack.core.node"


def hashable_value(value: Any) -> Any:
    if isinstance(value, FileStack):
        return value
    if isinstance(value, BaseModel):
        return hashable_inputs(value)
    if isinstance(value, list):
        return [hashable_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(hashable_value(item) for item in value)
    if isinstance(value, dict):
        return {key: hashable_value(item) for key, item in value.items()}
    return value


def hashable_inputs(arg: Any) -> dict[str, Any]:
    """
    Get the hashable inputs for the node. This allows exclusion of some fields from the hash.

    Returns:
        dict: The hashable inputs.
    """
    primary = type(arg).__primary_field__ if isinstance(arg, Model) else None
    values = {
        key: hashable_value(getattr(arg, key))
        for key in type(arg).model_fields
        if key != primary
    }
    # Scientific graphs carry materialized collection rows as members. They
    # affect execution even when their declared fields contain only record IDs.
    if "_cache" in vars(arg):
        values["_cache"] = hashable_value(vars(arg)["_cache"])
    return _HashableModel(
        model_type=f"{type(arg).__module__}.{type(arg).__qualname__}",
        fields=values,
        extras=hashable_value(arg.model_extra or {}),
    )


def compute_arg_hash(args: List[Model]) -> str:
    """
    Computes a hash for a list of arguments provided, where each argument
    is an instance of the Model class or can be processed into a hashable
    format. Uses a complex hashing function for the resulting computation.

    Args:
        args (List[Model]): A list of objects where each object must be an instance of the
            Model class. The objects are used to compute their respective
            hash values via a specified complex hashing mechanism.

    Returns:
        str: A string representation of the computed hash for the provided
            list of arguments.

    Raises:
        TypeError: If any item in the provided list is not an instance of the
            Model class.
    """
    arg_hashes = []
    for arg in args:
        if isinstance(arg, Model):
            arg_hash = (
                arg.complex_hash()
                if hasattr(arg, "complex_hash")
                else complex_hash_function(hashable_inputs(arg))
            )
            arg_hashes.append(arg_hash)
        else:
            raise TypeError(f"Argument {arg} is not an instance of {Model}")
    return cast(str, complex_hash_function(arg_hashes))
