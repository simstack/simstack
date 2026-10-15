"""Explicit, request-scoped file access for server-owned artifact execution.

This port contains no persistence implementation. API runners never install it:
their files remain bound to the active claim and the HTTP transfer client.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from simstack.models.files import FileStack
    from simstack.models.parameters import Resource


class FileIOProvider(Protocol):
    workdir: Path
    resource: Resource
    max_inline_document_bytes: int

    @property
    def staging_dir(self) -> Path: ...

    def materialize(self, file_stack: FileStack, local_dir: Path | None) -> Path: ...


_file_io: ContextVar[FileIOProvider | None] = ContextVar("simstack_file_io", default=None)


def current_file_io() -> FileIOProvider | None:
    return _file_io.get()


@contextmanager
def file_io_scope(provider: FileIOProvider):
    from simstack.core.context import context

    if context.initialized and context.runner_mode:
        raise RuntimeError("Server file I/O providers cannot replace an API runner's file boundary")
    token = _file_io.set(provider)
    try:
        yield provider
    finally:
        _file_io.reset(token)
