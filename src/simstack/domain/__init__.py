"""Pure domain services that do not depend on persistence or runtime context."""

from .dataset_templates import (
    freeze_dataset_metadata,
    reconcile_dataset_metadata,
)

__all__ = ["freeze_dataset_metadata", "reconcile_dataset_metadata"]
