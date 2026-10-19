"""Publish explicit file payloads before references cross the Runner API."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.models.files import FileStack
from simstack.runner_api.file_transfer import ExecutionFileTransferClient, FileTransferError
from simstack.runner_runtime.config import runner_task_workdir

from .references import walk_runtime_values
from .state import current_runner_claim


def publish_runtime_blobs(roots: Sequence[Any]) -> None:
    """Upload before commit/child resolution; failures prevent durable success."""
    if not context.initialized:
        return
    from .datasets import publish_runtime_datasets

    publish_runtime_datasets(roots)
    if not getattr(context.config, "object_storage", False):
        return
    from simstack.models.payloads import prepare_payloads

    prepare_payloads(roots)
    claim = current_runner_claim()
    client = ExecutionFileTransferClient.from_context()
    root = runner_task_workdir(Path(context.config.workdir), claim.task_id)
    root.mkdir(parents=True, exist_ok=True)
    published: dict[str, FileStack] = {}
    for value in walk_runtime_values(roots):
        if not isinstance(value, FileStack):
            continue
        if value.deleted_at is not None:
            raise FileTransferError("Cannot publish a deleted output FileStack")
        if value.blob is not None:
            # Server revalidates ownership/existence when accepting references.
            if value.blob.id != str(value.id):
                raise FileTransferError("Blob reference does not match FileStack identity")
            value.content = None
            value.in_memory = False
            value.locations = []
            continue
        previous = published.get(str(value.id))
        if previous is not None:
            if (value.name, value.size, value.is_hashable, value.hash) != (
                previous.name, previous.size, previous.is_hashable, previous.hash,
            ):
                raise FileTransferError("Conflicting output FileStack identities")
            canonical = previous
        else:
            with tempfile.TemporaryDirectory(prefix="publish-", dir=root) as directory:
                source = value.get(local_dir=Path(directory))
                canonical = client.publish_blob(
                    task_id=claim.task_id, claim_id=claim.claim_id, attempt=claim.attempt,
                    file_stack=value, source_path=source,
                )
            published[str(value.id)] = canonical
        for field in ("name", "size", "hash", "is_hashable", "blob", "in_memory", "content", "locations"):
            setattr(value, field, getattr(canonical, field))
