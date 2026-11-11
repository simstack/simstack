"""Pure, lossless scientific value graphs used by the execution API.

Graph references describe scientific values, never database collections or
partial document writes. Encoding and decoding perform no network or file I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from types import UnionType
import hashlib
import json
from typing import Annotated, Any, Callable, Literal, Union, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel, TypeAdapter, ValidationError

from simstack.models.document import Model
from simstack.models.document.fields import REFERENCE_SCHEMA_KEY
from simstack.models.document.schema import (escaped_identity_fields, escaped_computed_identity_fields,
                                            scientific_computed_names, scientific_field_names, validate_scientific_fields)
from simstack.models.document.codec import (
    TYPE_KEY,
    decode_document_value,
    encode_document_value,
)

TAG = "__simstack_value__"
_MAPPING_MARKERS = {TAG, TYPE_KEY, "__simstack_model__"}


def _mapping_key(key):
    return str(key.value if isinstance(key, Enum) else key)


def type_name(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def value_reference(value: Model) -> dict:
    return {"mapping": type_name(type(value)), "id": encode_document_value(value.id)}


def reference_key(mapping, identity):
    identity = encode_document_value(decode_document_value(identity))
    return mapping, json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)


def scientific_field_hashes(fields: dict) -> dict[str, str]:
    """Hash a typed scientific representation without retaining its field values."""
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            for name, value in fields.items() if name != "id"}


def _publication_objects(roots):
    """Choose record objects without conflating independent inline snapshots."""
    objects, seen = {}, set()

    def visit(value, reference=False):
        if isinstance(value, Model):
            key = reference_key(type_name(type(value)), value.id)
            if reference:
                objects[key] = value
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, BaseModel):
            for name, field in type(value).model_fields.items():
                if not field.exclude:
                    visit(getattr(value, name), bool((field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY)))
            for item in (value.model_extra or {}).values():
                visit(item)
            if "_cache" in vars(value):
                visit(vars(value)["_cache"], True)
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item, reference)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item, reference)

    for root in roots:
        visit(root, True)
    return objects


def encode_graph(roots: list[Model]) -> dict:
    nodes: dict[tuple[str, str], dict] = {}
    objects = _publication_objects(roots)
    active: set[tuple[str, str]] = set()
    missing = object()

    def fields(value: BaseModel, serialized=missing):
        cls = type(value)
        escaped = escaped_identity_fields(cls) or escaped_computed_identity_fields(cls)
        wire_names = scientific_field_names(cls)
        decorators = getattr(cls, "__pydantic_decorators__", None)
        serialized_fields = {
            name
            for decorator in getattr(decorators, "field_serializers", {}).values()
            if decorator.info.when_used not in {"json", "json-unless-none"}
            for name in decorator.info.fields
        }
        if serialized is missing or escaped:
            serialized = BaseModel.model_dump(
                value, mode="python", by_alias=not escaped, serialize_as_any=True
            )
        if cls.__pydantic_root_model__:
            # RootModel serializes its root directly, including arrays/scalars.
            # Keep the existing embedded envelope so its type and nested
            # scientific values survive storage without inventing a root field.
            if (cls.model_fields["root"].json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                return encode(value.root, reference=True)
            if "root" not in serialized_fields and "*" not in serialized_fields:
                encoded = encode(value.root, serialized)
            else:
                encoded = encode(serialized)
            # A dictionary root is data, not model fields or record metadata.
            if isinstance(value.root, Mapping) and isinstance(encoded, dict) and TAG not in encoded:
                return {TAG: "mapping", "items": list(encoded.items())}
            return encoded
        if not isinstance(serialized, dict):
            raise TypeError("Scientific model serialization must produce an object")
        declared = {
            (name if escaped else field.serialization_alias or field.alias or name): name
            for name, field in cls.model_fields.items()
        }
        extras = value.model_extra or {}
        result = {}
        for key, item in serialized.items():
            name = declared.get(key)
            # Pydantic decides which fields exist and their serialized values.
            # Their original types retain scientific reference envelopes.
            original = getattr(value, name) if name is not None else extras.get(key)
            if name is not None and (cls.model_fields[name].json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                # A reference presentation cannot rewrite the child's own data.
                encoded = encode(original, reference=True)
            elif name is not None and (
                name not in serialized_fields and "*" not in serialized_fields
                or isinstance(original, BaseModel) and isinstance(item, dict)
            ):
                encoded = encode(original, item)
            elif name is None and key in extras:
                # Extras are validated scientific values too. Pair their
                # original objects with Pydantic's serialized representation.
                encoded = encode(original, item)
            else:
                encoded = encode(item)
            if name is not None:
                key = wire_names[name]
            elif escaped and key in cls.model_computed_fields:
                key = scientific_computed_names(cls)[key]
            result[key] = encoded
        return result

    def members(value: BaseModel) -> dict:
        cache = vars(value).get("_cache")
        return {"members": encode(cache, reference=True)} if cache is not None else {}

    def encode(value, serialized=missing, *, reference=False):
        from simstack.models.node_registry import NodeRegistry

        if isinstance(value, NodeRegistry):
            return {TAG: "task", "id": str(value.id)}
        if isinstance(value, Model):
            ref = value_reference(value)
            key = reference_key(ref["mapping"], ref["id"])
            if reference:
                value = objects[key]
            else:
                return {TAG: "embedded", "mapping": ref["mapping"],
                        "value": fields(value, serialized), **members(value)}
            if key in active:
                raise ValueError("Cyclic scientific value graph")
            if key not in nodes:
                active.add(key)
                node = {**ref, "value": fields(value, serialized), **members(value)}
                if getattr(value, "_scientific_reference", None) == key:
                    node["revision"] = value._scientific_revision
                nodes[key] = node
                active.remove(key)
            return {TAG: "ref", **ref}
        if isinstance(value, BaseModel):
            return {
                TAG: "embedded",
                "mapping": type_name(type(value)),
                "value": fields(value, serialized),
                **members(value),
            }
        if serialized is not missing:
            if isinstance(value, Mapping) and isinstance(serialized, Mapping):
                paired = {_mapping_key(key): encode(value[key], item, reference=reference) if key in value else encode(item, reference=reference)
                          for key, item in serialized.items()}
                return ({TAG: "mapping", "items": list(paired.items())}
                        if _MAPPING_MARKERS.intersection(paired) else paired)
            if isinstance(value, (list, tuple, set, frozenset)) and isinstance(serialized, (list, tuple, set, frozenset)):
                if len(value) == len(serialized):
                    items = [encode(original, item, reference=reference) for original, item in zip(value, serialized)]
                    return {TAG: "tuple", "items": items} if isinstance(serialized, tuple) else items
            return encode(serialized, reference=reference)
        if isinstance(value, tuple):
            return {TAG: "tuple", "items": [encode(item, reference=reference) for item in value]}
        if isinstance(value, Mapping):
            if _MAPPING_MARKERS.intersection(value):
                return {
                    TAG: "mapping",
                    "items": [[_mapping_key(key), encode(item, reference=reference)] for key, item in value.items()],
                }
            return {_mapping_key(key): encode(item, reference=reference) for key, item in value.items()}
        if isinstance(value, (list, set, frozenset)):
            return [encode(item, reference=reference) for item in value]
        return encode_document_value(value)

    references = []
    for root in roots:
        if not isinstance(root, Model):
            raise TypeError("Scientific publications require Model roots")
        from simstack.models.node_registry import NodeRegistry

        if isinstance(root, NodeRegistry):
            raise TypeError(
                "Tasks are read-only execution references, not scientific publication roots"
            )
        encode(root, reference=True)
        references.append(value_reference(root))
    return {"roots": references, "nodes": list(nodes.values())}


def graph_values(value, *, fields=False, field_models=()):
    """Visit value envelopes without interpreting model field names as tags."""
    if isinstance(value, dict):
        if not fields:
            yield value
        for name, item in value.items():
            child_fields = (not fields and value.get(TAG) == "embedded" and name == "value"
                            and value.get("mapping") in field_models)
            yield from graph_values(item, fields=child_fields, field_models=field_models)
    elif isinstance(value, list):
        for item in value:
            yield from graph_values(item, field_models=field_models)


def graph_mappings(graph: dict, field_models=()) -> set[str]:
    result = {node["mapping"] for node in graph["nodes"]}
    for node in graph["nodes"]:
        for fields, value in ((True, node["value"]), (False, node.get("members"))):
            for item in graph_values(value, fields=fields, field_models=field_models):
                if item.get(TAG) == "embedded":
                    result.add(item["mapping"])
    return result


def decode_graph(
    graph: dict, resolve_type: Callable[[str], type], *, resolve_task=None
) -> list[Model]:
    nodes = {}
    for node in graph["nodes"]:
        key = reference_key(node["mapping"], node["id"])
        if key in nodes:
            raise ValueError("Duplicate scientific value identity")
        nodes[key] = node
    built: dict[tuple[str, str], Model] = {}
    active = set()

    def declared_value(annotation, value, discriminator=None):
        # GUI-created values may have no embedded graph marker. Their owner's
        # declared type still gives an exact interpretation of wire aliases.
        if isinstance(value, BaseModel):
            return value
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is Annotated:
            discriminator = discriminator or next((getattr(item, "discriminator", None)
                for item in args[1:] if getattr(item, "discriminator", None) is not None), None)
            return declared_value(args[0], value, discriminator)
        if origin in (Union, UnionType):
            targets = [target for target in args if target is not type(None)]
            if len(targets) == 1:
                return declared_value(targets[0], value)
            discriminator = getattr(discriminator, "discriminator", discriminator)
            # Ordinary object-model unions have the same wire-alias contract.
            # Do not infer a model branch against an arbitrary mapping/root:
            # that would rewrite literal data before Pydantic selects a type.
            model_union = all(isinstance(branch := (get_args(target)[0] if get_origin(target) is Annotated else target), type)
                              and issubclass(branch, BaseModel) and not branch.__pydantic_root_model__
                              for target in targets)
            if (isinstance(discriminator, str) or callable(discriminator) or model_union) and isinstance(value, dict):
                matches = []
                for target in targets:
                    branch = get_args(target)[0] if get_origin(target) is Annotated else target
                    fields = getattr(branch, "model_fields", {})
                    if (callable(discriminator) or discriminator is None) and any(
                        field.is_required() and not field.exclude
                        and (field.serialization_alias or field.alias or name) not in value
                        for name, field in fields.items()
                    ):
                        continue
                    if isinstance(discriminator, str):
                        fields = {discriminator: fields[discriminator]} if discriminator in fields else {}
                        if not fields:
                            continue
                    compatible = True
                    for name, field in fields.items():
                        if get_origin(field.annotation) is not Literal:
                            continue
                        wire_name = field.serialization_alias or field.alias or name
                        if wire_name not in value and not isinstance(discriminator, str):
                            continue
                        tag = encode_document_value(value.get(wire_name))
                        if not any(type(tag) is type(expected) and tag == expected for expected in
                                   (encode_document_value(item) for item in get_args(field.annotation))):
                            compatible = False
                            break
                    if compatible:
                        matches.append(target)
                if len(matches) == 1:
                    # Branch validation remains Pydantic's decision. Use
                    # only unambiguous declared field/Literal constraints to
                    # prepare aliases; never execute user code ahead of validators.
                    return declared_value(matches[0], value)
                if (callable(discriminator) or discriminator is None) and matches:
                    # Validators may distinguish values of the same shape.
                    # Apply only a common normalization; Pydantic still runs
                    # validation once, after the parent's before validators.
                    try:
                        normalized = [declared_value(target, value) for target in matches]
                    except (TypeError, ValueError):
                        return value
                    if all(same_alias_value(normalized[0], item) for item in normalized[1:]):
                        return normalized[0]
            return value
        if isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation.__pydantic_root_model__:
            field = annotation.model_fields["root"]
            root_annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
            return declared_value(root_annotation, value, field.discriminator)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
            return validation_fields(annotation, canonical_fields(annotation, value))
        if origin is dict and isinstance(value, dict) and len(args) == 2:
            return {key: declared_value(args[1], item) for key, item in value.items()}
        if origin in (list, set, frozenset) and isinstance(value, (list, tuple)) and args:
            return [declared_value(args[0], item) for item in value]
        if origin is tuple and isinstance(value, (list, tuple)):
            if len(args) == 2 and args[1] is Ellipsis:
                return tuple(declared_value(args[0], item) for item in value)
            return tuple(declared_value(args[index], item) if index < len(args) else item
                         for index, item in enumerate(value))
        return value

    def validation_fields(cls, values):
        # Keep ordinary dictionaries for parent before/wrap validators. Nested
        # validation can lose model_validate's by_name override across a wrap
        # validator, so use the declared model's normal constructor input keys.
        if cls.model_config.get("validate_by_name", cls.model_config.get("populate_by_name", False)) or cls.model_config.get("validate_by_alias") is False:
            return values
        missing = object()
        entries = []
        adapters = {}

        def scalar_annotation(annotation):
            origin, arguments = get_origin(annotation), get_args(annotation)
            if origin is Annotated:
                return scalar_annotation(arguments[0]) and not any(
                    callable(getattr(item, "func", None)) for item in arguments[1:])
            if origin in (list, tuple, dict, set, frozenset, Union, UnionType):
                return all(item is Ellipsis or scalar_annotation(item) for item in arguments)
            return annotation in (Any, type(None), bool, int, float, str, bytes, Decimal)

        def accepts(candidate, expected, field):
            if same_alias_value(candidate, expected):
                return True
            # One constructor input can legitimately yield int/float (or
            # bool/int) fields. Check the declared scalar conversion, without
            # executing model/field validators ahead of their normal order.
            if field is None or not scalar_annotation(field.annotation):
                return False
            if any(callable(getattr(item, "func", None)) for item in field.metadata):
                return False
            key = id(field)
            if key not in adapters:
                annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
                adapters[key] = TypeAdapter(annotation, config=cls.model_config)
            try:
                return same_alias_value(adapters[key].validate_python(candidate), expected)
            except ValidationError:
                return False

        for name, value in values.items():
            field = cls.model_fields.get(name)
            alias = field.validation_alias if field is not None else None
            aliases = alias.choices if isinstance(alias, AliasChoices) else [alias or name]
            entries.append((value, [item.path if isinstance(item, AliasPath) else [item]
                                    for item in aliases], field))

        def build(selected):
            # Reserve disjoint positive and negative index ranges before any
            # writes: x[0] and x[-1] need two cells, in either field order.
            sizes = {}
            for path, _, _ in selected:
                for index, key in enumerate(path):
                    if isinstance(key, int):
                        extents = sizes.setdefault(tuple(path[:index]), [0, 0])
                        side = int(key < 0)
                        extents[side] = max(extents[side], -key if side else key + 1)
            result = {}
            requirements = {}

            def put(target, path, value, field, prefix=()):
                key, *remaining = path
                if isinstance(target, list):
                    length = sum(sizes[prefix])
                    target.extend([missing] * max(0, length - len(target)))
                    existing = target[key]
                else:
                    existing = target.get(key, missing)
                if remaining:
                    expected = list if isinstance(remaining[0], int) else dict
                    if existing is missing:
                        existing = expected()
                        target[key] = existing
                    if not isinstance(existing, expected):
                        raise ValueError("Overlapping scientific validation alias paths")
                    put(existing, remaining, value, field, (*prefix, key))
                else:
                    required = requirements.setdefault((*prefix, key), [])
                    required.append((value, field))
                    candidates = [value] if existing is missing else [existing, value]
                    for candidate in candidates:
                        if all(accepts(candidate, expected, target_field) for expected, target_field in required):
                            target[key] = candidate
                            break
                    else:
                        raise ValueError("Overlapping scientific validation alias paths")

            for path, value, field in selected:
                put(result, path, value, field)
            return result

        def lookup(data, path):
            for key in path:
                if data is missing:
                    data = None  # Unassigned array cells become null below.
                if isinstance(data, str):
                    return missing
                try:
                    data = data[key]
                except (KeyError, IndexError, TypeError):
                    return missing
            return None if data is missing else data

        def choose(index, selected):
            if index == len(entries):
                return build(selected)
            value, choices, field = entries[index]
            for path in choices:
                candidate = [*selected, (path, value, field)]
                try:
                    data = build(candidate)
                    # AliasChoices uses the first present path, even when a
                    # different field supplied it. Never let it shadow a value.
                    for expected, alternatives, target_field in entries[:index + 1]:
                        actual = next((item for alternative in alternatives
                                       if (item := lookup(data, alternative)) is not missing), missing)
                        if not accepts(actual, expected, target_field):
                            raise ValueError("Overlapping scientific validation alias paths")
                    return choose(index + 1, candidate)
                except ValueError:
                    continue
            raise ValueError("Overlapping scientific validation alias paths")

        if all(len(choices) == 1 for _, choices, _ in entries):
            result = build([(choices[0], value, field) for value, choices, field in entries])
        else:
            result = choose(0, [])

        def fill(value):
            if value is missing:
                return None
            if isinstance(value, list):
                return [fill(item) for item in value]
            if isinstance(value, dict):
                return {key: fill(item) for key, item in value.items()}
            return value

        return fill(result)

    def same_alias_value(left, right):
        # JSON/Mongo may decode equal values as different Python objects. One
        # alias may serve both fields only if their typed values are identical;
        # ordinary equality would conflate True/1, Decimal/float or offsets.
        if left is right:
            return True
        if type(left) is not type(right):
            return False
        if isinstance(left, BaseModel):
            return (same_alias_value(vars(left), vars(right))
                    and same_alias_value(left.model_extra, right.model_extra))
        if isinstance(left, dict):
            return (left.keys() == right.keys()
                    and all(same_alias_value(left[key], right[key]) for key in left))
        if isinstance(left, (list, tuple)):
            return len(left) == len(right) and all(same_alias_value(a, b) for a, b in zip(left, right))
        try:
            return json.dumps(encode_document_value(left), sort_keys=True, allow_nan=False) == json.dumps(
                encode_document_value(right), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return False

    def canonical_fields(cls, fields):
        names = {wire: name for name, wire in scientific_field_names(cls).items()}
        computed = (set(cls.model_computed_fields) | set(scientific_computed_names(cls).values())) - set(names)
        values = {}
        for key, item in fields.items():
            if key in computed:
                continue
            name = names.get(key, key)
            field = cls.model_fields.get(name)
            if field is None:
                values[name] = item
                continue
            annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
            values[name] = declared_value(annotation, item, field.discriminator)
        return values

    def model(mapping, fields, members=None):
        cls = resolve_type(mapping)
        if not isinstance(cls, type) or not issubclass(cls, BaseModel):
            raise ValueError(f"Unknown scientific value type: {mapping}")
        if cls.__pydantic_root_model__:
            return cls.model_validate(declared_value(cls, decode(fields)), by_alias=False, by_name=True)
        computed = (set(cls.model_computed_fields) | set(scientific_computed_names(cls).values())) - set(scientific_field_names(cls).values())
        values = canonical_fields(cls, {key: decode(item) for key, item in fields.items() if key not in computed})
        # Leave untagged values as dictionaries so parent before-validators
        # still run before validation of their nested models.
        instance = validate_scientific_fields(cls, values)
        if members is not None:
            object.__setattr__(instance, "_cache", decode(members))
        return instance

    def reference(mapping, identity):
        key = reference_key(mapping, identity)
        if key in built:
            return built[key]
        if key in active:
            raise ValueError("Cyclic scientific value graph")
        if key not in nodes:
            raise ValueError(f"Missing scientific value: {mapping}:{identity}")
        active.add(key)
        node = nodes[key]
        instance = model(mapping, node["value"], node.get("members"))
        if not isinstance(instance, Model) or reference_key(mapping, instance.id) != key:
            raise ValueError("Scientific value identity does not match its graph")
        if "revision" in node:
            instance._scientific_revision = node["revision"]
            instance._scientific_reference = key
        active.remove(key)
        built[key] = instance
        return instance

    def decode(value):
        if isinstance(value, dict):
            kind = value.get(TAG)
            if kind == "ref":
                return reference(value["mapping"], value["id"])
            if kind == "embedded":
                return model(value["mapping"], value["value"], value.get("members"))
            if kind == "tuple":
                return tuple(decode(item) for item in value["items"])
            if kind == "mapping":
                return {key: decode(item) for key, item in value["items"]}
            if kind == "task":
                if resolve_task is None:
                    raise ValueError(
                        "Task references must be explicitly resolved before decoding"
                    )
                return resolve_task(str(value["id"]))
            if kind is not None:
                raise ValueError(f"Unknown scientific value envelope: {kind}")
            if TYPE_KEY in value:
                return decode_document_value(value)
            return {key: decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        return value

    return [reference(root["mapping"], root["id"]) for root in graph["roots"]]


def bind_publication_revisions(roots, graph):
    """Remember opaque versions for the submitted objects without changing their fields."""
    bind_read_revisions(roots, [{"mapping": node["mapping"], "id": node["id"], "revision": node["submitted_revision"]}
                               for node in graph["nodes"] if "submitted_revision" in node])


def read_revision_baselines(roots, graph):
    """Identify scientific serializers whose decoded read view encodes differently."""
    from simstack.models.files import FileStack
    from simstack.models.document.models import _MODEL_TYPES

    canonical = {reference_key(node["mapping"], node["id"]): node for node in graph["nodes"] if "revision" in node}
    if not canonical:
        return []
    requests = []
    for node in encode_graph(roots)["nodes"]:
        key = reference_key(node["mapping"], node["id"])
        if key not in canonical:
            continue
        expected = canonical[key]
        cls = _MODEL_TYPES[node["mapping"]]
        # Decoding fills optional defaults absent from the stored record. Bind
        # their read representation too, so an untouched default is not an edit
        # that can overwrite a concurrent writer's explicit value.
        computed_additions = set(scientific_computed_names(cls).values()) - set(expected["value"])
        read_fields = {name: value for name, value in node["value"].items() if name not in computed_additions}
        changed = {name for name, value in read_fields.items()
                   if name not in expected["value"] or expected["value"][name] != value}
        if issubclass(cls, FileStack):
            changed.difference_update(cls.model_json_schema()["x-simstack-file-storage-fields"].values())
        if changed:
            requests.append({"mapping": node["mapping"], "id": node["id"], "revision": expected["revision"],
                             "fields": scientific_field_hashes(read_fields)})
    return requests


def bind_read_revisions(roots, revisions):
    """Bind opaque edit revisions to the corresponding typed scientific objects."""
    versions = {reference_key(item["mapping"], item["id"]): item["revision"] for item in revisions}
    objects = _publication_objects(roots)
    seen = set()

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, Model):
            key = reference_key(type_name(type(value)), value.id)
            if key in versions and objects.get(key) is value:
                value._scientific_reference = key
                value._scientific_revision = versions[key]
        if isinstance(value, BaseModel):
            for name, field in type(value).model_fields.items():
                if not field.exclude:
                    visit(getattr(value, name))
            for child in (value.model_extra or {}).values():
                visit(child)
            if "_cache" in vars(value):
                visit(vars(value)["_cache"])
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                visit(child)

    visit(roots)
