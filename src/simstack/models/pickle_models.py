import base64
import logging
import sys
import types
from typing import Optional, Type, Any, Callable

import cloudpickle
from odmantic import Model

logger = logging.getLogger("PickleModels")


class _BytesB64Mixin:
    """
    Mixin that teaches Pydantic/ODMantic to serialise *bytes* fields as
    base-64-encoded ASCII strings when exporting to JSON (dict / response).
    """

    model_config = {
        "json_encoders": {bytes: lambda b: base64.b64encode(b).decode("ascii")}
    }


class ClassPickle(_BytesB64Mixin, Model):
    """
    Persist an *arbitrary Python class* in MongoDB.

    Fields
    ------
    name         – class __name__ (for reference / debugging)
    module_path  – original module path (dotted)
    pickle_data  – base64-encoded pickled bytes of the class

    Methods
    -------
    store_class(cls)
        Serialise and save the given class into `pickle_data`.
    load_class()
        Reconstruct the class object from `pickle_data`.
    """

    name: str
    module_path: str
    pickle_data: Optional[bytes] = None

    def store_class(self, cls: Type[Any]) -> None:
        self.name = cls.__name__
        self.module_path = cls.__module__
        self.pickle_data = cloudpickle.dumps(cls)
        logger.debug("Stored class %s (%s)", self.name, self.module_path)

    def load_class(self) -> Type[Any]:
        if self.pickle_data is None:
            raise ValueError("pickle_data is empty")
        return cloudpickle.loads(self.pickle_data)


class FunctionPickle(_BytesB64Mixin, Model):
    """
    Persist an *arbitrary Python function* in MongoDB.

    Fields
    ------
    name         – function __name__
    module_path  – original module path
    pickle_data  – base64 pickled bytes of the function
    """

    name: str
    module_path: str
    pickle_data: Optional[bytes] = None

    def _is_problematic_object(self, obj) -> bool:
        """Check if an object is problematic for pickling."""
        try:
            # Quick type checks
            # if isinstance(obj, (asyncio.Future, concurrent.futures.Future, asyncio.AbstractEventLoop)):
            #     return True
            #
            # Check type name for Windows-specific objects
            type_name = type(obj).__name__
            problematic_types = {
                "_OverlappedFuture",
                "ProactorEventLoop",
                "WindowsProactorEventLoopPolicy",
                "_ProactorBasePipeTransport",
                "_ProactorSocketTransport",
                "Handle",
                "_WindowsSelectorEventLoop",
                "_ProactorReadPipeTransport",
            }

            if type_name in problematic_types:
                return True

            # Check module origin
            if hasattr(obj, "__class__") and hasattr(obj.__class__, "__module__"):
                module_name = obj.__class__.__module__ or ""
                if any(x in module_name for x in ["_overlapped", "_winapi"]):
                    return True

            return False

        except Exception:
            return True  # If we can't inspect it safely, consider it problematic

    def _clean_object_recursively(self, obj, seen=None):
        """Recursively clean an object, removing problematic references."""
        if seen is None:
            seen = set()

        # Avoid infinite recursion
        obj_id = id(obj)
        if obj_id in seen:
            return obj
        seen.add(obj_id)

        try:
            if self._is_problematic_object(obj):
                return None

            # Handle different types
            if isinstance(obj, dict):
                cleaned = {}
                for key, value in obj.items():
                    if not self._is_problematic_object(value):
                        cleaned_value = self._clean_object_recursively(value, seen)
                        if cleaned_value is not None:
                            cleaned[key] = cleaned_value
                return cleaned

            elif isinstance(obj, (list, tuple)):
                cleaned_items = []
                for item in obj:
                    if not self._is_problematic_object(item):
                        cleaned_item = self._clean_object_recursively(item, seen)
                        if cleaned_item is not None:
                            cleaned_items.append(cleaned_item)
                return type(obj)(cleaned_items)

            # For other objects, try to pickle test
            try:
                cloudpickle.dumps(obj)
                return obj
            except Exception:
                return None

        except Exception:
            return None

    def _clean_globals(self, func_globals: dict) -> dict:
        """Remove unpickleable objects from function globals."""
        cleaned_globals = {}

        # Essential builtins that we need to keep
        essential_builtins = {
            "__builtins__",
            "__name__",
            "__doc__",
            "__package__",
            "print",
            "len",
            "str",
            "int",
            "float",
            "bool",
            "dict",
            "list",
            "tuple",
            "Exception",
            "ValueError",
            "TypeError",
            "AttributeError",
        }

        for key, value in func_globals.items():
            try:
                # Always skip certain names
                skip_names = {"_OverlappedFuture", "_overlapped", "_winapi"}

                if any(skip_name in key.lower() for skip_name in skip_names):
                    continue

                # Handle builtins specially
                if key == "__builtins__":
                    if isinstance(value, dict):
                        # Keep only essential builtins
                        cleaned_builtins = {
                            k: v
                            for k, v in value.items()
                            if k in essential_builtins
                            and not self._is_problematic_object(v)
                        }
                        cleaned_globals[key] = cleaned_builtins
                    continue

                # Skip modules entirely to avoid complex dependencies
                if isinstance(value, types.ModuleType):
                    continue

                # Deep clean the object
                cleaned_value = self._clean_object_recursively(value)
                if cleaned_value is not None:
                    # Final pickle test
                    try:
                        cloudpickle.dumps(cleaned_value)
                        cleaned_globals[key] = cleaned_value
                    except Exception as e:
                        logger.debug(
                            f"Skipping '{key}' after cleaning failed pickle test: {e}"
                        )
                else:
                    logger.debug(f"Skipping '{key}': cleaned to None")

            except Exception as e:
                logger.debug(f"Error processing global '{key}': {e}")
                continue

        return cleaned_globals

    def _clean_closure(self, closure):
        """Clean closure variables of problematic objects."""
        if not closure:
            return closure

        cleaned_closure = []
        for cell in closure:
            try:
                cell_contents = cell.cell_contents
                if not self._is_problematic_object(cell_contents):
                    cleaned_contents = self._clean_object_recursively(cell_contents)
                    if cleaned_contents is not None:
                        # Create new cell with cleaned contents
                        new_cell = types.CellType(cleaned_contents)
                        cleaned_closure.append(new_cell)
                    else:
                        # Create cell with None if we had to remove the contents
                        cleaned_closure.append(types.CellType(None))
                else:
                    # Replace problematic closure variable with None
                    cleaned_closure.append(types.CellType(None))
            except (ValueError, AttributeError):
                # Cell is empty or has issues, create empty cell
                cleaned_closure.append(types.CellType(None))
            except Exception as e:
                logger.debug(f"Error cleaning closure cell: {e}")
                cleaned_closure.append(types.CellType(None))

        return tuple(cleaned_closure) if cleaned_closure else None

    def store_function(self, func: Callable) -> None:
        self.name = func.__name__
        self.module_path = func.__module__

        try:
            # Clean the function's globals and closure
            cleaned_globals = self._clean_globals(func.__globals__)
            cleaned_closure = self._clean_closure(func.__closure__)

            # Create a copy of the function with cleaned components
            func_copy = types.FunctionType(
                func.__code__,
                cleaned_globals,
                func.__name__,
                func.__defaults__,
                cleaned_closure,
            )
            func_copy.__module__ = "__pickled_function__"

            # Final test - try to pickle the cleaned function
            test_pickle = cloudpickle.dumps(func_copy)

            self.pickle_data = test_pickle
            logger.debug("Stored function %s (%s)", self.name, self.module_path)

        except Exception as e:
            logger.error(f"Failed to pickle function {self.name}: {e}")
            self.pickle_data = None
            logger.warning(
                f"Function {self.name} will use regular import instead of pickle"
            )

    def load_function(self) -> Callable:
        if self.pickle_data is None:
            raise ValueError("pickle_data is empty")

        # Create a dummy module to satisfy cloudpickle's import requirements
        if "__pickled_function__" not in sys.modules:
            dummy_module = types.ModuleType("__pickled_function__")
            sys.modules["__pickled_function__"] = dummy_module

        func = cloudpickle.loads(self.pickle_data)

        # Ensure the function has the correct __name__ attribute
        if not hasattr(func, "__name__") or func.__name__ is None:
            func.__name__ = self.name
            logger.debug(f"Restored missing __name__ attribute: {self.name}")

        # Ensure other essential attributes are present
        if not hasattr(func, "__module__") or func.__module__ is None:
            func.__module__ = self.module_path
            logger.debug(f"Restored missing __module__ attribute: {self.module_path}")

        # Ensure __qualname__ exists (needed for some introspection)
        if not hasattr(func, "__qualname__"):
            func.__qualname__ = self.name

        logger.debug(
            "Loaded function %s (%s)", self.name, getattr(func, "__name__", "unnamed")
        )
        return func
