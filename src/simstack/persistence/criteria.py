"""Storage-neutral comparisons and ordering for domain repository requests.

Class field access only constructs this small value vocabulary. Translating it
to a database query is exclusively a Server adapter's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias


class Comparison(str, Enum):
    EQUAL = "eq"
    NOT_EQUAL = "ne"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    IN = "in"
    NOT_IN = "nin"
    EXISTS = "exists"
    MATCHES = "regex"


class Junction(str, Enum):
    ALL = "and"
    ANY = "or"


class SortDirection(str, Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


def _field_name(field: str) -> None:
    if not isinstance(field, str) or not field or field.strip() != field:
        raise ValueError("Criterion field must be a non-empty canonical name")
    if any(not part or part.startswith("$") for part in field.split(".")):
        raise ValueError("Invalid criterion field path")


class _Logical:
    def __and__(self, other):
        return all_of(self, other)

    def __or__(self, other):
        return any_of(self, other)

    def __bool__(self):
        raise TypeError("Combine criteria with &/| or all_of/any_of, not and/or")


@dataclass(frozen=True, slots=True)
class FieldCriterion(_Logical):
    field: str
    comparison: Comparison
    value: Any

    def __post_init__(self):
        _field_name(self.field)

    def to_dict(self) -> dict:
        return {"op": self.comparison.value, "field": self.field, "value": self.value}


@dataclass(frozen=True, slots=True)
class CriteriaGroup(_Logical):
    junction: Junction
    criteria: tuple[Criteria, ...]

    def __post_init__(self):
        if not self.criteria:
            raise ValueError("A criteria group requires at least one entry")

    def to_dict(self) -> dict:
        return {
            "op": self.junction.value,
            "items": [item.to_dict() for item in self.criteria],
        }


@dataclass(frozen=True, slots=True)
class Sort:
    field: str
    direction: SortDirection = SortDirection.ASCENDING

    def __post_init__(self):
        _field_name(self.field)

    def to_dict(self):
        return {
            "field": self.field,
            "direction": -1 if self.direction == SortDirection.DESCENDING else 1,
        }


Criteria: TypeAlias = FieldCriterion | CriteriaGroup


def eq(field, value):
    return FieldCriterion(field, Comparison.EQUAL, value)


def ne(field, value):
    return FieldCriterion(field, Comparison.NOT_EQUAL, value)


def lt(field, value):
    return FieldCriterion(field, Comparison.LESS_THAN, value)


def lte(field, value):
    return FieldCriterion(field, Comparison.LESS_THAN_OR_EQUAL, value)


def gt(field, value):
    return FieldCriterion(field, Comparison.GREATER_THAN, value)


def gte(field, value):
    return FieldCriterion(field, Comparison.GREATER_THAN_OR_EQUAL, value)


def in_values(field, values):
    return FieldCriterion(field, Comparison.IN, list(values))


def not_in_values(field, values):
    return FieldCriterion(field, Comparison.NOT_IN, list(values))


def all_of(*criteria):
    return CriteriaGroup(Junction.ALL, tuple(criteria))


def any_of(*criteria):
    return CriteriaGroup(Junction.ANY, tuple(criteria))


def ascending(field):
    return Sort(field, SortDirection.ASCENDING)


def descending(field):
    return Sort(field, SortDirection.DESCENDING)


def Criterion(op: str, field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison(op), value)


class FieldPath:
    """Compatibility notation for a typed field, with no storage operators."""

    __slots__ = ("path",)

    def __init__(self, path: str):
        _field_name(path)
        self.path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return FieldPath(f"{self.path}.{name}")

    def __eq__(self, value):
        return eq(self.path, value)

    def __ne__(self, value):
        return ne(self.path, value)

    def __lt__(self, value):
        return lt(self.path, value)

    def __le__(self, value):
        return lte(self.path, value)

    def __gt__(self, value):
        return gt(self.path, value)

    def __ge__(self, value):
        return gte(self.path, value)

    def in_(self, values):
        return in_values(self.path, values)

    def not_in(self, values):
        return not_in_values(self.path, values)

    def not_in_(self, values):
        return not_in_values(self.path, values)

    def nin(self, values):
        return not_in_values(self.path, values)

    def exists(self, value=True):
        return FieldCriterion(self.path, Comparison.EXISTS, bool(value))

    def match(self, pattern):
        if not isinstance(pattern, str):
            raise TypeError("A neutral text pattern must be a string")
        return FieldCriterion(self.path, Comparison.MATCHES, pattern)

    def asc(self):
        return ascending(self.path)

    def desc(self):
        return descending(self.path)

    def __str__(self):
        return self.path

    def to_dict(self):
        return {"field": self.path, "direction": 1}


def normalize_criteria(*criteria: Criteria | dict) -> dict:
    """Produce a validated neutral tree; raw database query dictionaries fail."""

    def normalize(value):
        if isinstance(value, (FieldCriterion, CriteriaGroup)):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise TypeError("Expected a neutral criterion")
        if not value:
            return {"op": "and", "items": []}
        op = value.get("op")
        if op in {"and", "or"}:
            if set(value) != {"op", "items"} or not isinstance(
                value["items"], (list, tuple)
            ):
                raise ValueError("Invalid criteria group")
            return {"op": op, "items": [normalize(item) for item in value["items"]]}
        if op not in {item.value for item in Comparison} or set(value) != {
            "op",
            "field",
            "value",
        }:
            raise ValueError(
                "Use typed neutral criteria instead of database query operators"
            )
        _field_name(value["field"])
        return dict(value)

    normalized = [normalize(item) for item in criteria]
    if len(normalized) == 1:
        return normalized[0]
    return {"op": "and", "items": normalized}


__all__ = [
    "Comparison",
    "Junction",
    "SortDirection",
    "FieldCriterion",
    "CriteriaGroup",
    "Criterion",
    "Criteria",
    "Sort",
    "FieldPath",
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in_values",
    "not_in_values",
    "all_of",
    "any_of",
    "ascending",
    "descending",
    "normalize_criteria",
]
