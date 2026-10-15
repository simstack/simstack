"""Persistence-free workflow node decorator and argument hashing.

Runner processes import this module while loading workflow code.  It therefore
contains no repository access or persistence implementation.  Non-runner node
execution is delegated to the executor injected into :mod:`simstack.core.context`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import (
    Any,
    Callable,
    List,
    Optional,
    ParamSpec,
    TypeVar,
    Union,
    cast,
    overload,
)

import coolname  # type: ignore[import-untyped]
from simstack.core.context import context
from simstack.core.hash import complex_hash_function
from simstack.domain.input_hashing import hashable_inputs, hashable_value
from simstack.models import Parameters
from simstack.models.document import Model


logger = logging.getLogger("Node")

P = ParamSpec("P")
T = TypeVar("T")


def default_name_generator() -> str:
    return "-".join(coolname.generate(2))


def compute_arg_hash(args: List[Model]) -> str:
    """Compute the stable cache hash for node arguments."""

    arg_hashes: list[str] = []
    for arg in args:
        if not isinstance(arg, Model):
            raise TypeError(f"Argument {arg} is not an instance of {Model}")
        arg_hash = (
            arg.complex_hash()
            if hasattr(arg, "complex_hash")
            else complex_hash_function(hashable_inputs(arg))
        )
        arg_hashes.append(arg_hash)
    return cast(str, complex_hash_function(arg_hashes))


def _parameters_field_values(parameters: Parameters) -> dict[str, Any]:
    raw_values = object.__getattribute__(parameters, "__dict__")
    return {
        field_name: raw_values[field_name]
        for field_name in Parameters.model_fields
        if field_name in raw_values
    }


def _parameters_from_node_kwargs(kwargs_node: dict[str, Any]) -> Parameters:
    base_parameters = kwargs_node.get("parameters")
    explicit_fields = (
        set(base_parameters.model_fields_set) if isinstance(base_parameters, Parameters)
        else set(base_parameters) if isinstance(base_parameters, dict) else set()
    )
    if isinstance(base_parameters, Parameters):
        values = _parameters_field_values(base_parameters)
    elif base_parameters is None:
        values = _parameters_field_values(Parameters())
    else:
        values = _parameters_field_values(Parameters.model_validate(base_parameters))

    for field_name in Parameters.model_fields:
        if field_name in kwargs_node:
            values[field_name] = kwargs_node[field_name]
            explicit_fields.add(field_name)

    result = Parameters.model_validate(values)
    object.__setattr__(result, "__pydantic_fields_set__", explicit_fields)
    return result


def _api_runner_mode() -> bool:
    return bool(context.runner_mode)


@overload
def node(_func: Callable[P, T]) -> Callable[..., T]: ...


@overload
def node(
    _func: None = None,
    *,
    name: Optional[str] = None,
    version: Optional[str] = None,
    cache: bool = True,
    **kwargs_node: Any,
) -> Callable[[Callable[P, T]], Callable[..., T]]: ...


def node(
    _func: Optional[Callable[P, T]] = None,
    *,
    version: Optional[str] = None,
    **kwargs_node: Any,
) -> Union[Callable[..., T], Callable[[Callable[P, T]], Callable[..., T]]]:
    """Mark a sync or async function as a SimStack workflow node."""

    def decorator(func: Callable[P, T]) -> Callable[..., T]:
        is_async = asyncio.iscoroutinefunction(func)

        setattr(func, "_is_node", True)
        setattr(func, "_node_version", version)
        if hasattr(func, "_node_code_version"):
            delattr(func, "_node_code_version")
        setattr(func, "_inner", func)
        setattr(func, "_node_parameters", _parameters_from_node_kwargs(kwargs_node))

        def update_kwargs(kwargs: dict[str, Any]) -> None:
            kwargs["func"] = func
            kwargs["is_async"] = is_async
            explicit_parameters = kwargs.pop("parameters", None)
            if explicit_parameters is None:
                kwargs["parameters"] = getattr(func, "_node_parameters").model_copy(
                    deep=True
                )
            elif isinstance(explicit_parameters, Parameters):
                kwargs["parameters"] = explicit_parameters.model_copy(deep=True)
            else:
                kwargs["parameters"] = Parameters.model_validate(
                    explicit_parameters
                ).model_copy(deep=True)
            kwargs["custom_name"] = kwargs.pop(
                "custom_name", kwargs_node.get("custom_name", default_name_generator())
            )
            if "call_path" not in kwargs and _api_runner_mode():
                from simstack.core.runner_execution.state import current_runner_claim
                kwargs["call_path"] = current_runner_claim().call_path
            call_path = kwargs.pop("call_path", "") or ""
            logger.debug("Task call_path: %s %s", call_path, func.__name__)
            kwargs["call_path"] = (
                f".{func.__name__}" if not call_path else f"{call_path}.{func.__name__}"
            )

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            update_kwargs(kwargs)
            if _api_runner_mode():
                from simstack.core.runner_execution import execute_runner_nested_node

                return cast(
                    T,
                    await execute_runner_nested_node(
                        func,
                        args,
                        kwargs,
                        version=version,
                    ),
                )
            return cast(
                T,
                await context.node_executor.execute_async(func, args, kwargs),
            )

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            update_kwargs(kwargs)
            if _api_runner_mode():
                from simstack.core.runner_execution import (
                    execute_runner_nested_node,
                )

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return cast(
                        T,
                        asyncio.run(
                            execute_runner_nested_node(
                                func,
                                args,
                                kwargs,
                                version=version,
                            )
                        ),
                    )
                # Preserve synchronous decorated workflows called from an
                # async node: run the API child on this same reentrant loop so
                # the parent's lease renewal continues while awaiting it.
                if not getattr(loop, "_nest_patched", False):
                    raise RuntimeError(
                        "Runner loop must be prepared before workflow invocation"
                    )
                return cast(
                    T, loop.run_until_complete(execute_runner_nested_node(
                        func, args, kwargs, version=version,
                    )),
                )
            return cast(T, context.node_executor.execute_sync(func, args, kwargs))

        setattr(async_wrapper, "is_node", True)
        setattr(sync_wrapper, "is_node", True)
        return cast(Callable[..., T], async_wrapper if is_async else sync_wrapper)

    setattr(decorator, "is_node", True)
    return decorator if _func is None else decorator(_func)


__all__ = [
    "compute_arg_hash",
    "default_name_generator",
    "hashable_inputs",
    "hashable_value",
    "node",
]
