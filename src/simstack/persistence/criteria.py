"""Backend-neutral repository criteria.

The domain package describes intent (field, comparison, value); concrete
adapters decide how to express that intent in their storage query language.
No MongoDB operators or driver types are exposed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias


class Comparison(str, Enum):
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    IN = "in"
    NOT_IN = "not_in"


class Junction(str, Enum):
    ALL = "all"
    ANY = "any"


class SortDirection(str, Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True)
class FieldCriterion:
    field: str
    comparison: Comparison
    value: Any

    def __post_init__(self) -> None:
        if not self.field or self.field.strip() != self.field:
            raise ValueError("Criterion field must be a non-empty canonical name")


@dataclass(frozen=True, slots=True)
class CriteriaGroup:
    junction: Junction
    criteria: tuple["Criteria", ...]

    def __post_init__(self) -> None:
        if len(self.criteria) < 2:
            raise ValueError("A criteria group requires at least two entries")


@dataclass(frozen=True, slots=True)
class Sort:
    field: str
    direction: SortDirection = SortDirection.ASCENDING

    def __post_init__(self) -> None:
        if not self.field or self.field.strip() != self.field:
            raise ValueError("Sort field must be a non-empty canonical name")


Criteria: TypeAlias = FieldCriterion | CriteriaGroup


def eq(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.EQUAL, value)


def ne(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.NOT_EQUAL, value)


def lt(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.LESS_THAN, value)


def lte(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.LESS_THAN_OR_EQUAL, value)


def gt(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.GREATER_THAN, value)


def gte(field: str, value: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.GREATER_THAN_OR_EQUAL, value)


def in_values(field: str, values: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.IN, tuple(values))


def not_in_values(field: str, values: Any) -> FieldCriterion:
    return FieldCriterion(field, Comparison.NOT_IN, tuple(values))


def all_of(*criteria: Criteria) -> CriteriaGroup:
    return CriteriaGroup(Junction.ALL, tuple(criteria))


def any_of(*criteria: Criteria) -> CriteriaGroup:
    return CriteriaGroup(Junction.ANY, tuple(criteria))


def ascending(field: str) -> Sort:
    return Sort(field, SortDirection.ASCENDING)


def descending(field: str) -> Sort:
    return Sort(field, SortDirection.DESCENDING)


__all__ = [
    "Comparison",
    "Criteria",
    "CriteriaGroup",
    "FieldCriterion",
    "Junction",
    "Sort",
    "SortDirection",
    "all_of",
    "any_of",
    "ascending",
    "descending",
    "eq",
    "gt",
    "gte",
    "in_values",
    "lt",
    "lte",
    "ne",
    "not_in_values",
]
