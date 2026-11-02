import asyncio
from datetime import datetime, timezone
import sys
from typing import Optional

import pytest

from simstack.models.document import EmbeddedModel, Model, ObjectId, Reference
from simstack.persistence.memory import InMemoryRepository
from simstack.runner_api.repository import HTTPRepository
from simstack.util.db import Database


class RepositoryChild(Model):
    value: int


class RepositoryValue(EmbeddedModel):
    label: str


class RepositoryParent(Model):
    child: RepositoryChild = Reference()
    value: RepositoryValue = Reference()
    title: str = "original"


class SpecificChild(RepositoryChild):
    detail: str


class SpecificValue(RepositoryValue):
    extra: int


class TupleReferences(Model):
    values: tuple[RepositoryChild, RepositoryParent] = Reference()


@pytest.mark.asyncio
async def test_changed_persisted_identity_rejects_entire_graph_before_writes():
    store = InMemoryRepository()
    value = RepositoryParent(
        child=RepositoryChild(value=1), value=RepositoryValue(label="inline")
    )
    await store.save(value)
    original_id = value.id
    value.id = ObjectId()
    value.child.value = 2
    with pytest.raises(ValueError, match="identity of a persisted document"):
        await store.save(value)
    persisted = await store.find_one(
        RepositoryParent, RepositoryParent.id == original_id
    )
    assert persisted.child.value == 1
    assert (
        await store.find_one(RepositoryParent, RepositoryParent.id == value.id) is None
    )


@pytest.mark.asyncio
async def test_reference_graph_and_partial_save_keep_concurrent_changes():
    store = InMemoryRepository()
    parent = RepositoryParent(
        child=RepositoryChild(value=1), value=RepositoryValue(label="inline")
    )
    await store.save(parent)
    runner = await store.find_one(RepositoryParent, RepositoryParent.id == parent.id)
    editor = await store.find_one(RepositoryParent, RepositoryParent.id == parent.id)
    editor.title = "UI edit"
    await store.save(editor)
    runner.child.value = 2
    await store.save(runner)
    loaded = await store.find_one(RepositoryParent, RepositoryParent.id == parent.id)
    assert loaded.title == "UI edit"
    assert loaded.child.value == 2
    assert loaded.value.label == "inline"
    raw = next(iter(store._store[RepositoryParent.__collection__].values()))
    assert raw["child"] == str(parent.child.id)
    assert raw["value"] == {"label": "inline"}


@pytest.mark.asyncio
async def test_reference_and_embedded_subclasses_keep_their_fields():
    store = InMemoryRepository()
    value = RepositoryParent(
        child=SpecificChild(value=2, detail="scientific subtype"),
        value=SpecificValue(label="nested", extra=7),
    )
    await store.save(value)
    restored = await store.find_one(RepositoryParent, RepositoryParent.id == value.id)
    assert type(restored.child) is SpecificChild
    assert restored.child.detail == "scientific subtype"
    assert type(restored.value) is SpecificValue
    assert restored.value.extra == 7


@pytest.mark.asyncio
async def test_nullable_reference_map_and_loaded_child_hook():
    class NullableReferences(Model):
        children: Optional[dict[str, RepositoryChild]] = Reference(default=None)

    class BaseWithHook(Model):
        model_config = {"collection": "hook_subtypes"}
        value: int

        async def db_find_postprocess(self, db):
            object.__setattr__(self, "_cache", -1)

    class ChildWithHook(BaseWithHook):
        label: str

        async def db_find_postprocess(self, db):
            object.__setattr__(self, "_cache", self.value * 2)

    store = InMemoryRepository()
    db = Database(repository=store)
    value = await db.save(NullableReferences(children={"one": RepositoryChild(value=3)}))
    restored = await db.find_one(NullableReferences, NullableReferences.id == value.id)
    assert restored.children["one"].value == 3
    empty = await db.save(NullableReferences())
    assert (await db.find_one(NullableReferences, NullableReferences.id == empty.id)).children is None
    child = await db.save(ChildWithHook(value=4, label="retained"))
    through_base = await db.find_one(BaseWithHook, BaseWithHook.id == child.id)
    assert type(through_base) is ChildWithHook
    assert through_base.label == "retained" and through_base._cache == 8


@pytest.mark.asyncio
async def test_cold_subtype_import_requires_exact_catalogue_entry(tmp_path, monkeypatch):
    from simstack.models.document import DocumentParsingError, SUBTYPE_KEY
    from simstack.models.models import ModelMapping
    from simstack.runner_api.repository import wire

    module = "cold_repository_subtype"
    marker = module + ".ColdChild"
    (tmp_path / (module + ".py")).write_text(
        f"from {RepositoryChild.__module__} import RepositoryChild\n"
        "class ColdChild(RepositoryChild):\n"
        f"    model_config = {{'collection': {RepositoryChild.__collection__!r}}}\n"
        "    detail: str\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    identity = str(ObjectId())

    class CatalogueAPI:
        authorized = False
        calls = []

        async def json(self, method, path, body):
            self.calls.append(body)
            if body["model"] == ModelMapping.__collection__:
                return {"documents": [{"mapping": marker}] if self.authorized else []}
            return {"documents": [wire({"id": identity, "value": 5, "detail": "cold", SUBTYPE_KEY: marker})]}

    api = CatalogueAPI()
    repository = HTTPRepository(api)
    assert module not in sys.modules
    with pytest.raises(DocumentParsingError, match="Unknown or incompatible"):
        await repository.find_one(RepositoryChild)
    assert module not in sys.modules
    api.authorized = True
    monkeypatch.setenv("SIMSTACK_REPOSITORY_REVISION", "a" * 64)
    try:
        value = await repository.find_one(RepositoryChild)
        assert type(value).__name__ == "ColdChild"
        assert value.value == 5 and value.detail == "cold"
        catalogue_call = next(call for call in reversed(api.calls) if call["model"] == "model_mapping")
        assert catalogue_call["filter"] == {"op": "eq", "field": "mapping", "value": marker}
        assert catalogue_call["repository_revision"] == "a" * 64
    finally:
        sys.modules.pop(module, None)


@pytest.mark.asyncio
async def test_heterogeneous_reference_tuple_keeps_declared_types():
    store = InMemoryRepository()
    parent = RepositoryParent(
        child=RepositoryChild(value=2), value=RepositoryValue(label="nested")
    )
    value = TupleReferences(values=(RepositoryChild(value=1), parent))
    await store.save(value)
    restored = await store.find_one(TupleReferences, TupleReferences.id == value.id)
    assert restored.values[0].value == 1
    assert restored.values[1].child.value == 2


class HookList(Model):
    elements: list[ObjectId] = []

    async def save(self, db):
        for child in getattr(self, "_cache", []):
            await db.save(child)
        self.elements = [child.id for child in getattr(self, "_cache", [])]
        await db.save_unchecked(self)

    async def db_find_postprocess(self, db):
        object.__setattr__(
            self,
            "_cache",
            await db.find(RepositoryChild, RepositoryChild.id.in_(self.elements)),
        )


class FailingLoadHook(Model):
    value: int

    async def db_find_postprocess(self, db):
        raise RuntimeError("load hook diagnostic")


@pytest.mark.asyncio
async def test_facade_retains_load_hook_diagnostic_without_losing_document(caplog):
    db = Database(repository=InMemoryRepository())
    value = await db.save(FailingLoadHook(value=5))
    restored = await db.find_one(FailingLoadHook, FailingLoadHook.id == value.id)
    assert restored.value == 5
    assert "load hook diagnostic" in caplog.text


@pytest.mark.asyncio
async def test_facade_keeps_private_save_and_load_hooks():
    db = Database(repository=InMemoryRepository())
    value = HookList()
    object.__setattr__(
        value, "_cache", [RepositoryChild(value=3), RepositoryChild(value=4)]
    )
    await db.save(value)
    loaded = await db.find_one(HookList, HookList.id == value.id)
    assert [item.value for item in loaded._cache] == [3, 4]


@pytest.mark.asyncio
async def test_only_one_concurrent_claim_succeeds():
    store = InMemoryRepository()
    identity = str(ObjectId())
    await store.save_documents(
        "node_registry",
        [
            {
                "document": {"id": identity, "status": "submitted"},
                "changed_fields": ["status"],
            }
        ],
    )
    attempts = await asyncio.gather(
        *(store.task_mutation(identity, "claim") for _ in range(10))
    )
    assert sum(result is not None for result in attempts) == 1


class ScientificPayload(Model):
    when: datetime
    data: bytes
    attributes: dict


@pytest.mark.asyncio
async def test_neutral_codec_round_trip_does_not_double_decode_user_mapping():
    store = InMemoryRepository()
    value = ScientificPayload(
        when=datetime(2026, 1, 2, tzinfo=timezone.utc),
        data=b"\x00\xff",
        attributes={"__simstack_type__": "date", "value": "user data"},
    )
    await store.save(value)
    restored = await store.find_one(ScientificPayload, ScientificPayload.id == value.id)
    assert restored.when == value.when
    assert restored.data == value.data
    assert restored.attributes == value.attributes


@pytest.mark.asyncio
async def test_api_sends_neutral_criteria_and_documents_only():
    class RecordingAPI:
        def __init__(self):
            self.calls = []

        async def json(self, method, path, body):
            self.calls.append((method, path, body))
            return (
                {"documents": body.get("documents", [])}
                if path.endswith("save")
                else {"documents": []}
            )

    api = RecordingAPI()
    store = HTTPRepository(api)
    await store.find(RepositoryChild, RepositoryChild.value >= 2)
    await store.save(RepositoryChild(value=3))
    assert api.calls[0][1] == "/runner/data/query"
    assert api.calls[0][2]["filter"]["op"] in {"gte", "all"}
    assert api.calls[0][2]["model_mapping"].endswith(".RepositoryChild")
    assert api.calls[1][1] == "/runner/data/save"
    assert api.calls[1][2]["documents"][0]["model_mapping"].endswith(".RepositoryChild")
    import json

    wire = json.dumps(api.calls)
    assert all(
        forbidden not in wire
        for forbidden in ("$set", "$lookup", "$match", "aggregate", "connection_string")
    )
