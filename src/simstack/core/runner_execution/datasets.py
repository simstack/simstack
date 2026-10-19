"""Stage dataset memberships without expanding root documents or dependency bundles."""

from __future__ import annotations

import json
import tempfile
from contextvars import ContextVar
from pathlib import Path

from simstack.models.dataset_storage import (
    SharedDatasetSectionStorage,
    dataset_json_value,
    dataset_root_revision,
    dataset_section_revision,
    section_rows,
)
from simstack.models.document import ObjectId
from simstack.runner_api.datasets import DatasetRunnerClient, MAX_DATASET_PAGE_BYTES

_publishing_datasets = ContextVar("simstack_publishing_datasets", default=frozenset())


def publish_runtime_datasets(roots):
    from simstack.core.context import context
    from simstack.core.runner_execution.references import walk_runtime_values
    from simstack.core.runner_execution.blobs import publish_runtime_blobs

    if not context.initialized or not getattr(context.config, "dataset_storage", False):
        return
    client = None
    for dataset in list(walk_runtime_values(roots)):
        if not hasattr(dataset, "sections") or getattr(
            type(dataset), "__dataset_kind__", None
        ) not in {"dict", "tuple"}:
            continue
        active = _publishing_datasets.get()
        if str(dataset.id) in active:
            raise ValueError("Cyclic dataset membership cannot be published")
        token = _publishing_datasets.set(active | {str(dataset.id)})
        try:
            client = _publish_sections(dataset, client, context, publish_runtime_blobs)
            dataset.revision = dataset_root_revision(dataset.model_dump_doc())
        finally:
            _publishing_datasets.reset(token)


def _publish_sections(dataset, client, context, publish_runtime_blobs):
    for key, section in dataset.sections.items():
        state = object.__getattribute__(section, "__dict__")
        object.__setattr__(section, "_dataset_section_key", key)
        same_owner = section.storage is None or section.storage.dataset_id == dataset.id
        if section.storage is not None and same_owner and state.get("_cache") is None:
            continue
        revision = dataset_section_revision(section.model_types, section_rows(section))
        if (
            section.storage is not None
            and same_owner
            and revision == section.storage.revision
        ):
            continue
        if client is None:
            client = DatasetRunnerClient.from_context()
        section_id = (
            section.storage.section_id
            if section.storage and same_owner
            else state.setdefault("_dataset_section_id", str(ObjectId()))
        )
        header = {
            "version": 1,
            "section_id": section_id,
            "model_types": section.model_types,
            "column_defs": section.column_defs,
            "expected_revision": section.storage.revision
            if section.storage and same_owner
            else None,
        }
        with tempfile.TemporaryDirectory(
            prefix="dataset-", dir=Path(context.config.workdir)
        ) as folder:
            source = Path(folder) / "snapshot.ndjson"
            with source.open("wb") as output:
                output.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
                cache = section._get_cache()
                for row in cache.values() if isinstance(cache, dict) else cache:
                    publish_runtime_blobs(
                        list(row.values()) if isinstance(row, dict) else list(row)
                    )
                for row in section_rows(section, with_documents=True):
                    encoded = json.dumps(
                        dataset_json_value(row), separators=(",", ":"), allow_nan=False
                    ).encode()
                    if len(encoded) > MAX_DATASET_PAGE_BYTES:
                        raise ValueError(
                            "Dataset membership row exceeds its staging byte budget"
                        )
                    output.write(encoded + b"\n")
            reply = client.snapshot(
                dataset.id, key, kind=dataset.__dataset_kind__, source=source
            )
        storage = (
            SharedDatasetSectionStorage.model_validate_doc(reply["storage"])
            if hasattr(SharedDatasetSectionStorage, "model_validate_doc")
            else SharedDatasetSectionStorage.model_validate(reply["storage"])
        )
        if (
            storage.dataset_id != dataset.id
            or storage.section_id != section_id
            or storage.revision
            != dataset_section_revision(section.model_types, section_rows(section))
            or storage.row_count != len(section._get_cache())
        ):
            raise ValueError(
                "Dataset snapshot response does not match its staged membership"
            )
        section.storage = storage
        section.inline_data = {} if dataset.__dataset_kind__ == "dict" else []
        section.table_entries = []
        section.column_defs = reply["column_defs"]
    return client
