"""Persistence ports and backend-neutral query criteria.

This package contains contracts only.  MongoDB adapters and database clients
belong to ``simstack-server`` and are intentionally absent from ``simstack``.
"""

from .criteria import (
    Comparison,
    Criteria,
    CriteriaGroup,
    FieldCriterion,
    Junction,
    Sort,
    SortDirection,
    all_of,
    any_of,
    ascending,
    descending,
    eq,
    gt,
    gte,
    in_values,
    lt,
    lte,
    ne,
    not_in_values,
)
from .ports import DocumentT, Repository


__all__ = [
    "Comparison",
    "Criteria",
    "CriteriaGroup",
    "DocumentT",
    "FieldCriterion",
    "Junction",
    "Repository",
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
