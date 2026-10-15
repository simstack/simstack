"""Ports implemented by server-owned persistence adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from simstack.models.document import Model

from .criteria import Criteria, Sort

if TYPE_CHECKING:
    from simstack.models.node_registry import NodeRegistry


DocumentT = TypeVar("DocumentT", bound=Model)


@runtime_checkable
class Repository(Protocol):
    """Minimal persistence port consumed by non-runner domain services.

    Runner processes must not receive an implementation of this protocol; they
    communicate with the server control plane instead.
    """

    async def find(
        self,
        model: type[DocumentT],
        *criteria: Criteria,
        sort: Sort | Sequence[Sort] | None = None,
        skip: int = 0,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[DocumentT]: ...

    async def find_one(
        self,
        model: type[DocumentT],
        *criteria: Criteria,
        **kwargs: Any,
    ) -> DocumentT | None: ...

    async def save(self, instance: DocumentT) -> DocumentT: ...

    async def update_node_registry(
        self, task_id: Any, changes: Mapping[str, Any], *,
        expected: Mapping[str, Any] | None = None,
    ) -> NodeRegistry | None:
        """Update explicit task fields only if the expected values still match."""
        ...

    async def add_node_parent(self, task_id: Any, parent_id: Any) -> NodeRegistry | None:
        """Atomically add a parent without duplicates or replacing task state."""
        ...

    async def delete(self, instance: Model) -> None: ...

    async def delete_all(self, model: type[Model]) -> None: ...


__all__ = ["DocumentT", "Repository"]
