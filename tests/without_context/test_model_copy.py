"""Copy model trees without losing Pydantic state or ODMantic identities."""

from typing import List, Optional

import pytest
from odmantic import EmbeddedModel, Model, Reference
from pydantic import BaseModel, PrivateAttr

from simstack.core.simstack_result import SimstackResult
from simstack.models.artifact_models import ArtifactModel
from simstack.util.model_copy import model_copy


class CopyReference(Model):
    name: str
    description: Optional[str] = None


class CopyEmbedded(EmbeddedModel):
    values: List[int]
    optional: Optional[str] = None


class CopyRoot(Model):
    first: CopyReference = Reference()
    second: CopyReference = Reference()
    embedded: CopyEmbedded
    values: List[int]
    optional: Optional[str] = None


@pytest.fixture
def original():
    child = CopyReference(name="shared")
    return CopyRoot(
        first=child, second=child, embedded=CopyEmbedded(values=[1]), values=[2]
    )


def test_default_copy_isolates_embedded_and_lists_but_keeps_database_references(
    original,
):
    copied = model_copy(original)
    assert copied is not original
    assert copied.id != original.id
    assert copied.first is original.first
    assert copied.second is original.first
    copied.values.append(3)
    copied.embedded.values.append(4)
    assert original.values == [2]
    assert original.embedded.values == [1]
    assert copied.model_dump_doc()["first"] == original.first.id


@pytest.mark.parametrize("preserve_ids", [False, True])
def test_explicit_reference_copy_preserves_sharing_and_valid_odmantic_state(
    original, preserve_ids
):
    copied = model_copy(original, copy_references=True, preserve_ids=preserve_ids)
    assert copied.first is copied.second
    assert copied.first is not original.first
    assert (copied.id == original.id) is preserve_ids
    assert (copied.first.id == original.first.id) is preserve_ids
    for model in (copied, copied.first, copied.embedded):
        assert isinstance(model.__fields_modified__, set)
        assert model.model_dump_doc(include=model.__fields_modified__)
    assert copied.optional is None
    assert copied.embedded.optional is None


def test_copy_restores_deleted_optional_defaults(original):
    del original.__dict__["optional"]
    del original.embedded.__dict__["optional"]
    copied = model_copy(original)
    assert copied.optional is None
    assert copied.embedded.optional is None
    assert copied.model_dump_doc()["optional"] is None
    assert "optional" not in original.__dict__
    assert "optional" not in original.embedded.__dict__


def test_deep_false_constructs_a_new_shallow_root(original):
    copied = model_copy(original, deep=False)
    assert copied is not original
    assert copied.id != original.id
    assert copied.values is original.values
    assert copied.embedded is original.embedded
    assert copied.first is original.first


def test_copy_preserves_simstack_result_outputs():
    original = SimstackResult(answer=ArtifactModel(name="answer", data={"energy": -1}))
    copied = model_copy(original)
    assert "answer" in copied.__pydantic_extra__
    assert copied.answer is original.answer


def test_copy_preserves_private_model_state():
    class WithState(BaseModel):
        name: str
        _cache: dict = PrivateAttr(default_factory=dict)

    original = WithState(name="x")
    original._cache["value"] = [5]
    copied = model_copy(original)
    assert copied._cache == original._cache
    assert copied._cache is not original._cache


def test_copy_handles_a_valid_pydantic_alias():
    from pydantic import Field

    class WithAlias(BaseModel):
        value: str = Field(alias="storedValue")

    original = WithAlias(storedValue="x")
    assert model_copy(original).value == "x"


def test_shallow_copy_keeps_reference_and_embedded_bookkeeping_unchanged(original):
    object.__setattr__(original.first, "__fields_modified__", set())
    object.__setattr__(original.embedded, "__fields_modified__", set())
    copied = model_copy(original, deep=False, preserve_ids=True)
    assert copied is not original
    assert copied.id == original.id
    assert original.first.__fields_modified__ == set()
    assert original.embedded.__fields_modified__ == set()
    assert copied.__fields_modified__ == set(type(copied).model_fields)


def test_deep_copy_does_not_restore_defaults_on_retained_references(original):
    del original.first.__dict__["description"]
    object.__setattr__(original.first, "__fields_modified__", set())
    source_values = original.first.__dict__.copy()
    copied = model_copy(original)
    assert copied.first is original.first
    assert original.first.__dict__ == source_values
    assert original.first.__fields_modified__ == set()


def test_copy_does_not_rerun_model_validators():
    from pydantic import model_validator

    class Normalized(BaseModel):
        name: str

        @model_validator(mode="after")
        def normalize(self):
            self.name += "!"
            return self

    original = Normalized(name="value")
    assert model_copy(original).name == original.name == "value!"


def test_copy_preserves_model_and_container_cycles():
    from typing import Any

    class RecursiveData(BaseModel):
        items: list[Any]

    original = RecursiveData(items=[])
    original.items.append(original)
    copied = model_copy(original)
    assert copied is not original
    assert copied.items[0] is copied

    cyclic_list = []
    cyclic_tuple = (cyclic_list,)
    cyclic_list.append(cyclic_tuple)
    copied_tuple = model_copy(cyclic_tuple)
    assert copied_tuple is not cyclic_tuple
    assert copied_tuple[0][0] is copied_tuple


def test_copy_preserves_shared_containers_across_fields_and_extra_state():
    from pydantic import ConfigDict

    class ExtraData(BaseModel):
        model_config = ConfigDict(extra="allow")
        values: list[int]

    original = ExtraData(values=[1])
    original.additional_values = original.values
    copied = model_copy(original)
    assert copied.values == [1]
    assert copied.values is not original.values
    assert copied.additional_values is copied.values


def test_copy_preserves_custom_odmantic_primary_key_only_when_requested():
    from odmantic import Field

    class CustomKey(Model):
        key: str = Field(primary_field=True)
        value: int

    original = CustomKey(key="identity", value=7)
    with pytest.raises(ValueError, match="preserve_ids=True"):
        model_copy(original)
    copied = model_copy(original, preserve_ids=True)
    assert copied is not original
    assert copied.key == original.key
    assert copied.model_dump_doc()["_id"] == "identity"


def test_copy_does_not_silently_fill_missing_required_values(original):
    del original.__dict__["values"]
    with pytest.raises(ValueError, match="missing required field 'values'"):
        model_copy(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_references", [False, True])
async def test_odmantic_saves_copied_models_and_references(
    original, monkeypatch, copy_references
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from mongomock_motor import AsyncMongoMockClient
    from odmantic import AIOEngine

    @asynccontextmanager
    async def mock_session():
        yield None

    client = AsyncMongoMockClient()
    # Only sessions are unavailable in mongomock; retain ODMantic's actual
    # recursive save and dirty-field serialization rather than replacing save.
    monkeypatch.setattr(client, "start_session", AsyncMock(side_effect=mock_session))
    engine = AIOEngine(client=client, database="model_copy_test")
    await engine.save(original)
    copied = model_copy(original, copy_references=copy_references)
    copied.embedded.values.append(9)
    await engine.save(copied)

    roots = engine.get_collection(CopyRoot)
    references = engine.get_collection(CopyReference)
    source_doc = await roots.find_one({"_id": original.id})
    copied_doc = await roots.find_one({"_id": copied.id})
    assert source_doc["embedded"]["values"] == [1]
    assert copied_doc["embedded"]["values"] == [1, 9]
    assert copied_doc["first"] == copied_doc["second"] == copied.first.id
    assert await references.find_one({"_id": copied.first.id}) is not None
    assert await roots.count_documents({}) == 2
    assert await references.count_documents({}) == (2 if copy_references else 1)
