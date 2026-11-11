import hashlib
import inspect
from collections.abc import Callable, Iterable, Mapping, Set
from typing import Any, cast

from pydantic import BaseModel
from simstack.models.document import ObjectId, Model

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
    return isinstance(obj, (int, float, bool, type(None)))


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
    return str(ComplexHash(iterable).complex_hash(iterable))


def _hash_parts(kind: str, parts: Iterable[HashResult]) -> str:
    """Frame each component so neither boundaries nor structure can collide."""
    digest = hashlib.sha256()
    for part in (kind,):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _value_type(obj: Any) -> str:
    # Mutable and immutable byte buffers deliberately share the raw-byte hash.
    if isinstance(obj, (bytes, bytearray)):
        return "builtins.bytes"
    return type(obj).__module__ + "." + type(obj).__qualname__


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
        self._active_ids: set[int] = set()
        self._class_hashes: dict[type[Any], str] = {}

    def _typed_hash(self, obj: Any) -> str:
        return _hash_parts(_value_type(obj), [self.complex_hash(obj)])

    def hash_dict(self, obj: Mapping[Any, Any]) -> str:
        entries = sorted(
            (self._typed_hash(key), self._typed_hash(value))
            for key, value in obj.items()
        )
        return _hash_parts("mapping:" + _value_type(obj),
                           (_hash_parts("entry", entry) for entry in entries))

    def hash_class(self, cls_obj: Any) -> str:
        # check is the class name starts with a name in the hash_exclusions list
        class_type = cls_obj.__class__
        class_name = class_type.__module__ + "." + class_type.__name__
        if class_type.__module__ == "builtins":
            return "builtin"
        if any([class_name.startswith(exclusion) for exclusion in hash_exclusions]):
            return "excluded"
        # print("hashing class", cls_obj.__class__.__name__)
        if class_type not in self._class_hashes:
            self._class_hashes[class_type] = hash_class_def(class_type)
        class_hash = self._class_hashes[class_type]
        # TODO is __dict__ better than vars ?
        # dict_hash = self.hash_dict(vars(cls_obj))

        if isinstance(cls_obj, BaseModel):
            # Repository snapshots and private caches describe transport state,
            # not scientific inputs. Inspect declared values for every depth of
            # inheritance, retaining each concrete subtype's scientific fields.
            obj_dict = {name: cls_obj.__dict__[name]
                        for name in type(cls_obj).model_fields if name in cls_obj.__dict__}
            if isinstance(cls_obj, Model):
                obj_dict.pop(type(cls_obj).__primary_field__, None)
        else:
            obj_dict = cls_obj.__dict__.copy()

        dict_hash = self.hash_dict(obj_dict)
        if hasattr(cls_obj, "model_extra") and cls_obj.model_extra is not None:
            dict_hash = dict_hash + self.hash_dict(cls_obj.model_extra)
        combined_hash = class_hash + dict_hash
        return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()

    def complex_hash(self, obj: Any) -> HashResult:
        # TODO: are the functions of a class hashed correctly?

        if isinstance(obj, type):
            return hash_class_def(obj)
        elif isinstance(obj, (bytes, bytearray)):
            # hash() of bytes is salted per process (PYTHONHASHSEED).
            return hashlib.sha256(bytes(obj)).hexdigest()
        elif is_primitive_type(obj):
            value = obj.hex() if isinstance(obj, float) else repr(obj)
            return _hash_parts(_value_type(obj), [value])
        elif isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(
            obj, str
        ):  # strings are iterable but we want to hash them directly
            return _hash_parts("str", [obj])
        elif inspect.isfunction(obj):
            return hash_function_body(obj)

        identity = id(obj)
        if identity in self._active_ids:
            raise ValueError("Cannot hash cyclic scientific input")
        self._active_ids.add(identity)
        try:
            if inspect.ismethod(obj):
                return self.complex_hash(obj.__self__)
            if isinstance(obj, Mapping):
                return self.hash_dict(obj)
            if hasattr(obj, "__dict__"):
                if hasattr(obj, "complex_hash"):
                    return cast(HashResult, obj.complex_hash())
                return self.hash_class(obj)
            if is_iterable(obj):
                values = (self._typed_hash(item) for item in obj)
                if isinstance(obj, Set):
                    values = iter(sorted(values))
                return _hash_parts("iterable:" + _value_type(obj), values)
            return _hash_parts(_value_type(obj), [hash_value(obj)])
        finally:
            self._active_ids.remove(identity)


def complex_hash_function(obj: Any) -> HashResult:
    return ComplexHash(obj).complex_hash(obj)
