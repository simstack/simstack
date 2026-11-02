import asyncio
from datetime import datetime, timezone

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
