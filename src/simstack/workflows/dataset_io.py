"""Dataset pages prepared by Server, confined to one artifact worker's files root."""

from __future__ import annotations
import json
from pathlib import Path

MAX_DATASET_PAGE_BYTES = 4 * 1024 * 1024


class PreparedDatasetIO:
    def __init__(self, request):
        self.request = request
        self.root = Path(request["files_root"]).resolve(strict=True)
        self.sections = {}
        for entry in request["dataset_io"]["sections"]:
            key = (entry["dataset_id"], entry["section_id"], entry["revision"])
            if key in self.sections:
                raise ValueError("Duplicate prepared dataset snapshot")
            self.sections[key] = entry

    def page(self, storage, *, kind, section_key, offset, limit):
        entry = self.sections.get(
            (str(storage.dataset_id), storage.section_id, storage.revision)
        )
        if entry is None or entry["kind"] != kind:
            raise ValueError(
                "Dataset snapshot is absent from the scoped worker inventory"
            )
        descriptor = next(
            (
                page
                for page in entry["pages"]
                if page["offset"] <= offset < page["offset"] + page["row_count"]
            ),
            None,
        )
        if descriptor is None:
            raise ValueError("Prepared dataset snapshot has a membership gap")
        relative = Path(descriptor["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Prepared dataset page path is not confined")
        path = (self.root / relative).resolve(strict=True)
        if not path.is_relative_to(self.root) or (self.root / relative).is_symlink():
            raise ValueError("Prepared dataset page escapes its scoped directory")
        with path.open("rb") as source:
            raw = source.read(MAX_DATASET_PAGE_BYTES + 1)
        if len(raw) > MAX_DATASET_PAGE_BYTES:
            raise ValueError("Prepared dataset page exceeds its byte budget")
        page = json.loads(raw)
        start = offset - descriptor["offset"]
        rows = page["rows"][start : start + limit]
        return {
            **page,
            "rows": rows,
            "offset": offset,
            "next_offset": offset + len(rows)
            if offset + len(rows) < page["row_count"]
            else None,
        }

    def hydrate_documents(self, entries):
        from .runtime import _runtime_classes, _validate_graph
        from simstack.models.dataset_storage import bind_dataset_sections

        classes, _, _ = _runtime_classes(self.request)
        documents, roots = {}, []
        for entry in entries:
            mapping, value = entry["variable_mapping"], entry["value"]
            roots.append({"mapping": mapping, "id": value["id"]})
            for item in [entry, *entry["dependencies"]]:
                path, document = item["variable_mapping"], item["value"]
                documents[path, document["id"]] = {
                    "mapping": path,
                    "id": document["id"],
                    "document": document,
                }
        mappings = []
        for mapping in {key[0] for key in documents}:
            if mapping not in classes:
                raise ValueError(
                    "Dataset member model is absent from the pinned workflow catalogue"
                )
            mappings.append(
                {"mapping": mapping, "collection_name": classes[mapping].__collection__}
            )
        _, graph, _, _ = _validate_graph(
            {
                **self.request,
                "documents": list(documents.values()),
                "mappings": mappings,
                "roots": roots,
            }
        )
        values = {identity: model for (_, identity), model in graph.items()}
        bind_dataset_sections(list(values.values()))
        return {root["id"]: graph[root["mapping"], root["id"]] for root in roots}
