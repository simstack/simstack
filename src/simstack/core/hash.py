import hashlib
import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from pydantic import BaseModel

from simstack.domain.input_hashing import model_field_values
from simstack.models.document import Model, ObjectId

hash_exclusions = [
    "pandas",
    "numpy",
    "ase",
    "indigo",
    "openbabel",
    "pybel",
    "pymatgen",
    "ase",
    "numpy",
    "sqlalchemy",
]


HashResult = str | int


def is_iterable(obj: Any) -> bool:
    try:
        iter(obj)
        return True
    except TypeError:
        return False


def is_primitive_type(obj: Any) -> bool:
    return isinstance(obj, (int, float, bytes, bool, bytearray, type(None)))


def hash_value(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def hash_class_def(cls: type[Any]) -> str:
    try:
        source_code = inspect.getsource(cls)
        source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
        return source_hash
    except Exception:
        mro = cls.__mro__
        for mro_class in mro:
            try:
                source_code = inspect.getsource(mro_class)
                source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
                return source_hash
            except Exception:
                pass
        return "no source code"


def hash_iterable(iterable: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for item in iterable:
        digest.update(str(complex_hash_function(item)).encode("utf-8"))
    return digest.hexdigest()


def hash_function_body(func: Callable[..., Any]) -> str:
    # Get the source code of the function
    source_code = inspect.getsource(func)
    # Compute the hash of the source code
    hash_object = hashlib.sha256(source_code.encode("utf-8"))
    hash_digest = hash_object.hexdigest()
    return hash_digest


def hash_non_callable_members(instance: Any) -> dict[str, str]:
    hashed_values: dict[str, str] = {}
    for attr_name, attr_value in vars(instance).items():
        if not callable(attr_value):
            hashed_values[attr_name] = hashlib.sha256(
                str(attr_value).encode()
            ).hexdigest()
    return hashed_values


class ComplexHash:
    def __init__(self, obj: Any) -> None:
        self.hash_history: list[Any] = []

    def hash_dict(self, obj: Mapping[Any, Any]) -> str:
        hashed_values: dict[Any, HashResult] = {}
        for k, v in obj.items():
            hashed_values[k] = self.complex_hash(v)
        combined_hash = "".join(f"{k}:{v}" for k, v in sorted(hashed_values.items()))
        return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()

    def hash_class(self, cls_obj: Any) -> str:
        # check is the class name starts with a name in the hash_exclusions list
        class_type = cls_obj.__class__
        class_name = class_type.__module__ + "." + class_type.__name__
        if class_type.__module__ == "builtins":
            return "builtin"
        if any([class_name.startswith(exclusion) for exclusion in hash_exclusions]):
            return "excluded"
        # print("hashing class", cls_obj.__class__.__name__)
        class_hash = hash_class_def(cls_obj.__class__)
        if isinstance(cls_obj, BaseModel):
            # Model extras are intentional input data, included exactly once.
            # Persistence/runtime attributes outside declared fields never
            # influence either regular or custom complex-hash input paths.
            obj_dict = model_field_values(
                cls_obj, exclude_identity=isinstance(cls_obj, Model)
            )
        else:
            obj_dict = cls_obj.__dict__.copy()
        dict_hash = self.hash_dict(obj_dict)
        combined_hash = class_hash + dict_hash
        return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()

    def complex_hash(self, obj: Any) -> HashResult:
        # TODO: are the functions of a class hashed correctly?

        if isinstance(obj, type):
            return hash_class_def(obj)
        elif isinstance(obj, (bytes, bytearray)):
            # Python's hash(bytes) is salted per process. Parent and detached
            # workers must agree on identical binary input without a fixed seed.
            digest = hashlib.sha256()
            digest.update(type(obj).__name__.encode("ascii") + b"\0")
            digest.update(obj)
            return digest.hexdigest()
        elif is_primitive_type(obj):
            return hash(obj)
        elif isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(
            obj, str
        ):  # strings are iterable but we want to hash them directly
            return hash_value(obj)
        elif inspect.isfunction(obj):
            return hash_function_body(obj)
        elif inspect.ismethod(obj):
            if hasattr(obj, "__self__"):
                if obj in self.hash_history:
                    return "recursive"
                self.hash_history.append(obj)
                if hasattr(obj, "complex_hash"):
                    return cast(HashResult, obj.complex_hash())
                return self.hash_class(obj.__self__)
            else:
                return hash_function_body(obj)
        # this should be instantiated classes
        elif hasattr(obj, "__dict__"):
            if obj in self.hash_history:
                return "recursive"
            self.hash_history.append(obj)
            if hasattr(obj, "complex_hash"):
                return cast(HashResult, obj.complex_hash())
            return self.hash_class(obj)
        elif isinstance(obj, dict):
            return self.hash_dict(obj)
        elif is_iterable(obj):
            digest = hashlib.sha256()
            for i, item in enumerate(obj):
                digest.update(f"{i}:{self.complex_hash(item)}".encode("utf-8"))
            return digest.hexdigest()
        else:
            return hash_value(obj)


def complex_hash_function(obj: Any) -> HashResult:
    return ComplexHash(obj).complex_hash(obj)
