"""Serialize models and ODMantic queries for remote database API transport."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, Union

from bson import ObjectId as BsonObjectId
from odmantic import Model, ObjectId


def model_fqn(model_class: Type[Model]) -> str:
    return f"{model_class.__module__}.{model_class.__name__}"


def serialize_query(query: Any) -> Optional[Dict[str, Any]]:
    if query is None:
        return None
    if isinstance(query, dict):
        return _json_safe_dict(dict(query))
    raise TypeError(f"Unsupported query type: {type(query)!r}")


def serialize_find_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return _json_safe_dict(kwargs)


def _json_safe_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _json_safe_value(value) for key, value in data.items()}


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, (ObjectId, BsonObjectId)):
        return str(value)
    if isinstance(value, dict):
        return _json_safe_dict(value)
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    return value


def serialize_model(instance: Model) -> Dict[str, Any]:
    data = instance.model_dump(mode="json", by_alias=True)
    return _json_safe_dict(data)


def deserialize_model(model_class: Type[Model], data: Dict[str, Any]) -> Model:
    prepared = _restore_object_ids(data)
    return model_class.model_validate(prepared)


def _restore_object_ids(data: Any) -> Any:
    if isinstance(data, dict):
        restored: Dict[str, Any] = {}
        for key, value in data.items():
            if key in ("id", "_id") and isinstance(value, str):
                try:
                    restored[key] = ObjectId(value)
                    continue
                except Exception:
                    pass
            restored[key] = _restore_object_ids(value)
        return restored
    if isinstance(data, list):
        return [_restore_object_ids(item) for item in data]
    return data


def serialize_models(instances: List[Model]) -> List[Dict[str, Any]]:
    return [serialize_model(instance) for instance in instances]
