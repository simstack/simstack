"""JSON-schema helpers for persistence-neutral domain models."""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel

from .fields import (
    PAYLOAD_SCHEMA_KEY,
    REFERENCE_MODEL_SCHEMA_KEY,
    REFERENCE_SCHEMA_KEY,
)


def scientific_field_names(model_class: type) -> dict[str, str]:
    """Reserve the API's identity slot without changing user field declarations."""
    primary = getattr(model_class, "__primary_field__", None)
    names = {name: "id" if name == primary else field.serialization_alias or field.alias or name
             for name, field in model_class.model_fields.items()}
    if primary is not None:
        occupied = set(names) | set(names.values()) | set(model_class.model_computed_fields)
        occupied.update(field.alias for field in model_class.model_computed_fields.values() if field.alias)
        for name, wire in names.items():
            if name != primary and wire == "id":
                while wire in occupied:
                    wire += "_"
                names[name] = wire
                occupied.add(wire)
    return names


def escaped_identity_fields(model_class: type) -> dict[str, str]:
    primary = getattr(model_class, "__primary_field__", None)
    return {name: wire for name, wire in scientific_field_names(model_class).items()
            if name != primary and wire != (model_class.model_fields[name].serialization_alias
                                           or model_class.model_fields[name].alias or name)}


def scientific_computed_names(model_class: type) -> dict[str, str]:
    names = {name: field.alias or name for name, field in model_class.model_computed_fields.items()}
    if getattr(model_class, "__primary_field__", None) is not None:
        fields = scientific_field_names(model_class)
        occupied = set(fields) | set(fields.values()) | set(names) | set(names.values())
        for name, wire in names.items():
            if wire == "id":
                while wire in occupied:
                    wire += "_"
                names[name] = wire
                occupied.add(wire)
    return names


def escaped_computed_identity_fields(model_class: type) -> dict[str, str]:
    return {name: wire for name, wire in scientific_computed_names(model_class).items()
            if wire != (model_class.model_computed_fields[name].alias or name)}


def scientific_core_schema(model_class: type, core_schema: dict, *, canonical=False) -> dict:
    """Escape aliases for schema generation only; constructor/dump contracts stay public."""
    escaped = escaped_identity_fields(model_class)
    computed = escaped_computed_identity_fields(model_class)
    if not escaped and not computed:
        return core_schema

    def rewrite(schema):
        if schema.get("type") == "model-fields":
            fields = dict(schema["fields"])
            for name, wire in escaped.items():
                fields[name] = {**fields[name], "serialization_alias": wire,
                                "validation_alias": name if canonical else wire}
            return {**schema, "fields": fields, "computed_fields": [
                {**field, "alias": computed[field["property_name"]]}
                if field["property_name"] in computed else field
                for field in schema.get("computed_fields", [])]}
        if isinstance(schema.get("schema"), dict):
            return {**schema, "schema": rewrite(schema["schema"])}
        return schema

    return rewrite(core_schema)


def validate_scientific_fields(model_class: type, values):
    """Validate decoded names without a user alias consuming the identity again."""
    if escaped_identity_fields(model_class):
        from pydantic_core import SchemaValidator

        # A wrap validator can reset Pydantic's runtime by_alias override.
        # A private validator with canonical aliases survives that boundary,
        # including user before/after validators, without changing the class.
        validator = SchemaValidator(scientific_core_schema(
            model_class, model_class.__pydantic_core_schema__, canonical=True))
        return validator.validate_python(values, by_alias=False, by_name=True)
    return model_class.model_validate(values, by_alias=False, by_name=True)


def validate_scientific_document(model_class: type, values):
    """Accept escaped fields throughout a hydrated document, in normal validator order."""
    from pydantic_core import SchemaValidator, core_schema

    changed = False

    def visit(value):
        nonlocal changed
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        # The model's ref lives outside all its wrap/before/after validators.
        # Normalize at that boundary, without running any child validators early.
        target = value
        while isinstance(target, dict) and target.get("type") != "model":
            target = target.get("schema")
        cls = target.get("cls") if isinstance(target, dict) else None
        escaped = escaped_identity_fields(cls) if cls is not None and "ref" in value else {}
        computed = (set(scientific_computed_names(cls).values()) - set(scientific_field_names(cls).values())
                    if cls is not None and "ref" in value else set())
        if escaped or computed:
            changed = True
            value = scientific_core_schema(cls, value, canonical=True)
        result = {key: visit(item) for key, item in value.items()}
        if escaped or computed:
            def normalize(data):
                if isinstance(data, Mapping):
                    data = {key: item for key, item in data.items() if key not in computed}
                    for name, wire in escaped.items():
                        if wire in data:
                            data[name] = data.pop(wire)
                return data

            result = core_schema.no_info_before_validator_function(
                normalize, result, ref=result.pop("ref"))
        return result

    schema = visit(model_class.__pydantic_core_schema__)
    if changed:
        return SchemaValidator(schema).validate_python(values)
    return model_class.model_validate(values)


def _annotate_reference_schema(
    annotation: Any, schema: dict[str, Any], *, field_path: str
) -> None:
    """Mark document leaves, not an array/map that happens to contain them."""
    for key in (
        REFERENCE_SCHEMA_KEY,
        REFERENCE_MODEL_SCHEMA_KEY,
    ):
        schema.pop(key, None)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        _annotate_reference_schema(arguments[0], schema, field_path=field_path)
        return
    if origin in (Union, UnionType):
        candidates = [item for item in arguments if item is not type(None)]
        if len(candidates) != 1:
            raise TypeError(
                f"{field_path} must reference exactly one document type per union"
            )
        target = candidates[0]
        branches = [
            item
            for item in schema.get("anyOf", [])
            if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(branches) != 1:
            raise TypeError(
                f"{field_path} has no unambiguous nullable reference schema"
            )
        # A nullable scalar still denotes one reference. Preserve its public
        # field-level marker while leaving the schema's null branch untouched.
        if isinstance(target, type) and hasattr(target, "__primary_field__"):
            _annotate_reference_schema(target, schema, field_path=field_path)
            _annotate_reference_schema(target, branches[0], field_path=field_path)
            return
        _annotate_reference_schema(target, branches[0], field_path=field_path)
        return
    if isinstance(annotation, type) and hasattr(annotation, "__primary_field__"):
        schema[REFERENCE_SCHEMA_KEY] = True
        schema[REFERENCE_MODEL_SCHEMA_KEY] = annotation.__name__
        return
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        # Legacy declarations sometimes spell Reference() for a value object
        # (notably Parameters). Its identity-free value is embedded as before.
        return
    if origin in (list, set, frozenset):
        child = schema.get("items")
        if len(arguments) != 1 or not isinstance(child, dict):
            raise TypeError(f"{field_path} has an invalid reference array schema")
        _annotate_reference_schema(arguments[0], child, field_path=f"{field_path}[]")
        return
    if origin is dict:
        children = [schema.get("additionalProperties"), *schema.get("patternProperties", {}).values()]
        children = [child for child in children if isinstance(child, dict)]
        if len(arguments) != 2 or not children:
            raise TypeError(f"{field_path} has an invalid reference map schema")
        for child in children:
            _annotate_reference_schema(arguments[1], child, field_path=f"{field_path}{{}}")
        return
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            child = schema.get("items")
            if not isinstance(child, dict):
                raise TypeError(f"{field_path} has an invalid reference tuple schema")
            _annotate_reference_schema(
                arguments[0], child, field_path=f"{field_path}[]"
            )
            return
        children = schema.get("prefixItems")
        if not isinstance(children, list) or len(children) != len(arguments):
            raise TypeError(f"{field_path} has an invalid fixed reference tuple schema")
        for index, (target, child) in enumerate(zip(arguments, children)):
            if not isinstance(child, dict):
                raise TypeError(f"{field_path} has an invalid reference tuple item")
            _annotate_reference_schema(
                target, child, field_path=f"{field_path}[{index}]"
            )
        return
    # A fixed tuple may mix referenced records with primitive coordinates or
    # labels. Those positions retain their primitive schemas and values.
    return


def annotate_document_schema(
    model_class: type,
    schema: dict[str, Any],
    *,
    mode: str = "validation",
    by_alias: bool = True,
) -> dict[str, Any]:
    """Describe actual reference targets independently from decorative titles."""

    if getattr(model_class, "__pydantic_root_model__", False):
        schema["x-simstack-root-model"] = True
        field = model_class.model_fields["root"]
        if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
            _annotate_reference_schema(field.annotation, schema, field_path=f"{model_class.__name__}.root")
    dataset_kind = getattr(model_class, "__dataset_kind__", None)
    if dataset_kind and "sections" in model_class.model_fields:
        schema["x-simstack-dataset"] = {"version": 1, "kind": dataset_kind}
    artifact_kind = getattr(model_class, "__simstack_artifact_kind__", None)
    if artifact_kind:
        schema["x-simstack-artifact-kind"] = artifact_kind
    table_kind = getattr(model_class, "__simstack_table_kind__", None)
    if table_kind or artifact_kind in {"table", "chart"}:
        schema["x-simstack-table-payload"] = table_kind or artifact_kind
    selection_kind = getattr(model_class, "__dataset_selection_kind__", None)
    if selection_kind:
        schema["x-simstack-dataset-selection"] = {"version": 1, "kind": selection_kind}

    payload_fields = getattr(model_class, "__payload_fields__", ())
    if payload_fields:
        schema[PAYLOAD_SCHEMA_KEY] = {
            "version": 1,
            "fields": [dict(field) for field in payload_fields],
        }

    properties = schema.get("properties")
    if isinstance(properties, dict):
        primary_name = getattr(model_class, "__primary_field__", None)
        wire_names = scientific_field_names(model_class)
        escaped = escaped_identity_fields(model_class)
        computed_names = scientific_computed_names(model_class)
        aliases = {}
        renamed = {}
        for name, field in model_class.model_fields.items():
            wire_name = wire_names[name]
            schema_name = name
            if by_alias:
                alias = field.serialization_alias if mode == "serialization" else field.validation_alias
                if isinstance(alias, str):
                    schema_name = alias
                elif isinstance(alias, (AliasChoices, AliasPath)):
                    choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
                    for choice in choices:
                        if isinstance(choice, str):
                            schema_name = choice
                            break
                        if len(choice.path) == 1 and isinstance(choice.path[0], str):
                            schema_name = choice.path[0]
                            break
                if name in escaped:
                    schema_name = escaped[name]
            if schema_name in properties:
                renamed[schema_name] = wire_name
            if wire_name != name:
                aliases[name] = wire_name
        if mode == "serialization":
            escaped_computed = escaped_computed_identity_fields(model_class)
            for name, wire_name in computed_names.items():
                schema_name = (escaped_computed.get(name, model_class.model_computed_fields[name].alias or name)
                               if by_alias else name)
                if schema_name in properties:
                    renamed[schema_name] = wire_name
        # Use the original properties once: aliases can form swaps or chains,
        # and an earlier rename must not become a later field's source schema.
        properties = {renamed.get(name, name): value for name, value in properties.items()}
        schema["properties"] = properties
        if "required" in schema:
            schema["required"] = [renamed.get(name, name) for name in schema["required"]]
        if aliases:
            schema["x-simstack-field-aliases"] = aliases
        identity = model_class.model_fields.get(primary_name)
        if identity is not None:
            if primary_name != "id":
                schema["x-simstack-primary-field"] = primary_name
        computed = sorted(set(computed_names.values()) - set(wire_names.values()))
        if computed:
            schema["x-simstack-computed-fields"] = computed
        for field_name, field in model_class.model_fields.items():
            if not (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                continue
            field_schema = properties.get(wire_names[field_name])
            if isinstance(field_schema, dict):
                _annotate_reference_schema(
                    field.annotation,
                    field_schema,
                    field_path=f"{model_class.__name__}.{field_name}",
                )

    target_model: type | None = None
    for base in getattr(model_class, "__orig_bases__", ()):
        origin = get_origin(base)
        if getattr(origin, "__name__", None) != "ObjectListMixin":
            continue
        arguments = get_args(base)
        if len(arguments) == 1 and isinstance(arguments[0], type):
            target_model = arguments[0]
            break

    if target_model is None:
        return schema

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    elements = properties.get("elements")
    if not isinstance(elements, dict):
        return schema
    items = elements.get("items")
    if not isinstance(items, dict):
        return schema

    target_name = target_model.__name__
    items[REFERENCE_SCHEMA_KEY] = True
    items[REFERENCE_MODEL_SCHEMA_KEY] = target_name
    items.setdefault("title", f"{target_name} reference")
    return schema


def annotate_nested_plain_schemas(model_class, schema, handler, domain_base):
    """Normalize plain value definitions reached through actual field schemas."""
    generator = handler.generate_json_schema
    normalized = getattr(generator, "_simstack_plain_schemas", None)
    if normalized is None:
        # Definitions are shared by different enclosing Domain hooks in one
        # generation. Retain their identity so an alias swap runs only once.
        normalized = generator._simstack_plain_schemas = {}

    def fields(cls, fragment):
        if cls.__pydantic_root_model__:
            field = cls.model_fields["root"]
            annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
            visit(annotation, fragment, field.discriminator)
            return
        wire_names = scientific_field_names(cls)
        for name, field in cls.model_fields.items():
            wire = wire_names[name]
            child = fragment.get("properties", {}).get(wire)
            if isinstance(child, dict):
                annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
                visit(annotation, child, field.discriminator)

    def visit(annotation, fragment, discriminator=None):
        if not isinstance(fragment, dict):
            return
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is Annotated:
            discriminator = discriminator or next((getattr(item, "discriminator", None)
                for item in args[1:] if getattr(item, "discriminator", None) is not None), None)
            visit(args[0], fragment, discriminator)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            try:
                resolved = handler.resolve_ref_schema(fragment)
            except LookupError:
                # A surrounding recursive definition is still being built;
                # its enclosing Domain hook visits it once it is available.
                return
            mode = annotation.model_config.get("json_schema_mode_override") or handler.mode
            key = (annotation, id(resolved), mode)
            if key in normalized:
                return
            normalized[key] = resolved
            if not issubclass(annotation, domain_base):
                annotate_document_schema(annotation, resolved,
                    mode=mode, by_alias=generator.by_alias)
            fields(annotation, resolved)
        elif origin in (Union, UnionType):
            choices = fragment.get("anyOf", fragment.get("oneOf", []))
            targets = [target for target in args if target is not type(None)]
            choices = [child for child in choices if child.get("type") != "null"]
            for target, child in zip(targets, choices):
                visit(target, child)
            discriminator = getattr(discriminator, "discriminator", discriminator)
            if isinstance(discriminator, str) and "discriminator" in fragment:
                wire_names = set()
                for target in targets:
                    branch = get_args(target)[0] if get_origin(target) is Annotated else target
                    field = getattr(branch, "model_fields", {}).get(discriminator)
                    if field is not None:
                        wire_names.add(field.serialization_alias or field.alias or discriminator)
                if len(wire_names) == 1:
                    fragment["discriminator"]["propertyName"] = wire_names.pop()
                elif wire_names:
                    # Different wire keys have no common discriminator. The
                    # oneOf schemas still describe each branch exactly.
                    fragment.pop("discriminator")
        elif origin in (list, set, frozenset):
            if args and isinstance(fragment.get("items"), dict):
                visit(args[0], fragment["items"])
        elif origin is dict:
            if len(args) == 2:
                for child in [fragment.get("additionalProperties"), *fragment.get("patternProperties", {}).values()]:
                    if isinstance(child, dict):
                        visit(args[1], child)
        elif origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                if isinstance(fragment.get("items"), dict):
                    visit(args[0], fragment["items"])
            else:
                for target, child in zip(args, fragment.get("prefixItems", [])):
                    visit(target, child)

    normalized[(model_class, id(schema), handler.mode)] = schema
    fields(model_class, schema)
    return schema


__all__ = ["annotate_document_schema", "annotate_nested_plain_schemas"]
