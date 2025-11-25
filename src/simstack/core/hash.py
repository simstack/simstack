import hashlib
import inspect

from odmantic import ObjectId

max_iterable_hash_count = 10000

hash_exclusions = [
    "pandas",
    "numpy",
    "ase",
    "rdkit",
    "openbabel",
    "pybel",
    "rdkit",
    "pymatgen",
    "ase",
    "numpy",
    "sqlalchemy",
]


def is_iterable(obj):
    try:
        iter(obj)
        return True
    except TypeError:
        return False


def is_primitive_type(obj):
    return isinstance(obj, (int, float, bytes, bool, bytearray, type(None)))


def hash_value(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def hash_class_def(cls):
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


def hash_iterable(iterable):
    hash_value = b""
    for count, item in enumerate(iterable):
        hash_value += complex_hash_function(item)
        if count > max_iterable_hash_count:
            break
    return hashlib.sha256(hash_value.encode("utf-8")).hexdigest()


def hash_function_body(func):
    # Get the source code of the function
    source_code = inspect.getsource(func)
    # Compute the hash of the source code
    hash_object = hashlib.sha256(source_code.encode("utf-8"))
    hash_digest = hash_object.hexdigest()
    return hash_digest


def hash_non_callable_members(instance):
    hashed_values = {}
    for attr_name, attr_value in vars(instance).items():
        if not callable(attr_value):
            hashed_values[attr_name] = hashlib.sha256(
                str(attr_value).encode()
            ).hexdigest()
    return hashed_values


class ComplexHash:
    def __init__(self, obj):
        self.hash_history = []

    def hash_dict(self, obj):
        hashed_values = {}
        for k, v in obj.items():
            hashed_values[k] = self.complex_hash(v)
        combined_hash = "".join(f"{k}:{v}" for k, v in sorted(hashed_values.items()))
        return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()

    def hash_class(self, cls_obj):
        # check is the class name starts with a name in the hash_exclusions list
        class_type = cls_obj.__class__
        class_name = class_type.__module__ + "." + class_type.__name__
        if class_type.__module__ == "builtins":
            return "builtin"
        if any([class_name.startswith(exclusion) for exclusion in hash_exclusions]):
            return "excluded"
        # print("hashing class", cls_obj.__class__.__name__)
        class_hash = hash_class_def(cls_obj.__class__)
        # TODO is __dict__ better than vars ?
        # dict_hash = self.hash_dict(vars(cls_obj))

        obj_dict = cls_obj.__dict__.copy()
        if hasattr(cls_obj, "__class__") and hasattr(cls_obj.__class__, "__bases__"):
            for base in cls_obj.__class__.__bases__:
                if base.__name__ == "Model" and "odmantic.model" in str(
                    base.__module__
                ):
                    if "id" in obj_dict:
                        del obj_dict["id"]

        dict_hash = self.hash_dict(obj_dict)
        if hasattr(cls_obj, "model_extra") and cls_obj.model_extra is not None:
            dict_hash = dict_hash + self.hash_dict(cls_obj.model_extra)
        if hasattr(cls_obj, "model_fields") and cls_obj.model_fields is not None:
            dict_hash = dict_hash + self.hash_dict(cls_obj.model_fields)
        combined_hash = class_hash + dict_hash
        return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()

    def complex_hash(self, obj):
        # TODO: are the functions of a class hashed correctly?

        if isinstance(obj, type):
            return hash_class_def(obj)
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
                    return obj.complex_hash()
                return self.hash_class(obj.__self__)
            else:
                return hash_function_body(obj)
        # this should be instantiated classes
        elif hasattr(obj, "__dict__"):
            if obj in self.hash_history:
                return "recursive"
            self.hash_history.append(obj)
            if hasattr(obj, "complex_hash"):
                return obj.complex_hash()
            return self.hash_class(obj)
        elif isinstance(obj, dict):
            return self.hash_dict(obj)
        elif is_iterable(obj):
            hashed_values = {}
            count = 0
            for i, item in enumerate(obj):
                hashed_values[i] = self.complex_hash(item)
                count += 1
                if count > max_iterable_hash_count:
                    break
            combined_hash = "".join(
                f"{k}:{v}" for k, v in sorted(hashed_values.items())
            )
            return hashlib.sha256(combined_hash.encode("utf-8")).hexdigest()
        else:
            return hash_value(obj)


def complex_hash_function(obj):
    return ComplexHash(obj).complex_hash(obj)
