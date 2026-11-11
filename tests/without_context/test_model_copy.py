"""Copy model trees without losing Pydantic state or document identities."""

from typing import List, Optional

import pytest
from simstack.models.document import EmbeddedModel, Model, Reference
from pydantic import BaseModel, PrivateAttr

from simstack.core.simstack_result import SimstackResult
from simstack.models.artifact_models import ArtifactModel
from simstack.util.model_copy import model_copy
from simstack.util.object_transfer import ObjectFileClient

_OBJECT_CLIENT_FROM_CONTEXT = ObjectFileClient.from_context


@pytest.mark.parametrize("deep,preserve_ids,copy_references", [
    (True, False, True), (True, True, True), (True, False, False), (False, False, True),
])
@pytest.mark.parametrize("partial", [False, True])
def test_object_list_copy_keeps_ids_aligned_with_its_materialized_members(deep, preserve_ids, copy_references, partial):
    from simstack.models import StringData
    from simstack.models.base_lists import StringDataList
    members = [StringData(value="first"), StringData(value="second")]
    original = StringDataList(elements=members)
    if partial:
        original._set_cache(members[:1])
    copied = model_copy(original, deep=deep, preserve_ids=preserve_ids, copy_references=copy_references)
    for index, member in enumerate(copied._get_cache()):
        assert copied.elements[index] == member.id
        assert copied[index] is member
        changed = deep and copy_references and not preserve_ids
        assert (member.id != members[index].id) == changed
    assert original.elements == [member.id for member in members]
    if partial:
        assert copied.elements[1] == members[1].id


def test_staged_copy_serializes_with_upload_cleanup(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    from pathlib import Path
    import shutil
    import threading
    from types import SimpleNamespace
    from simstack.core.context import context
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance

    monkeypatch.setattr(context, "_config", SimpleNamespace(workdir=tmp_path, resource="local"))
    source = FileStack.from_string("shared scientific bytes", "input.txt")
    entered, resume, upload_started = threading.Event(), threading.Event(), threading.Event()
    original = shutil.copyfile
    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(shutil, "copyfile", paused)
    class Client:
        object_api_prefix = "/runner/objects"
        def publish(self, value, path):
            assert path.read_text() == "shared scientific bytes"
            return FileInstance(path="object", resource="__s3__", created_at=datetime.now(),
                location_type="s3", bucket="files", object_key="object").model_dump()
    def upload():
        upload_started.set()
        source.ensure_uploaded(client=Client())
    with ThreadPoolExecutor(2) as executor:
        copying = executor.submit(model_copy, source)
        try:
            assert entered.wait(5)
            uploading = executor.submit(upload)
            assert upload_started.wait(5)
            assert not uploading.done()
        finally:
            resume.set()
        copied = copying.result(timeout=5)
        uploading.result(timeout=5)
    assert Path(copied.locations[0].path).read_text() == "shared scientific bytes"


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("preserve_ids", [False, True])
@pytest.mark.parametrize("constructor", ["local", "string"])
def test_file_snapshot_copies_own_their_bytes_across_publication(tmp_path, monkeypatch, deep, preserve_ids, constructor):
    from datetime import datetime
    from pathlib import Path
    from types import SimpleNamespace
    from simstack.core.context import context
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance

    monkeypatch.setattr(context, "_config", SimpleNamespace(workdir=tmp_path, resource="local"))
    source = tmp_path / "input.txt"
    source.write_text("original input")
    original = (FileStack.from_string("original input", source.name) if constructor == "string"
                else FileStack.from_local_file(source))
    copied = model_copy(original, deep=deep, preserve_ids=preserve_ids)
    original_staging, copied_staging = Path(original._staging_directory), Path(copied._staging_directory)
    assert original_staging != copied_staging
    assert (original.id == copied.id) is preserve_ids
    source.unlink()

    class Uploaded:
        object_api_prefix = "/runner/objects"

        def publish(self, file_stack, path):
            assert path.read_text() == "original input"
            return FileInstance(path=str(file_stack.id), resource="__s3__", created_at=datetime.now(),
                location_type="s3", bucket="bucket", object_key=str(file_stack.id),
                checksum_sha256=file_stack.hash, size_bytes=file_stack.size).model_dump()

    original.ensure_uploaded(client=Uploaded())
    assert not original_staging.exists() and copied_staging.exists()
    assert copied.get_raw("local", tmp_path / "copied").read_text() == "original input"
    copied.ensure_uploaded(client=Uploaded())
    assert not copied_staging.exists()


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
def test_explicit_reference_copy_preserves_sharing_and_pydantic_state(
    original, preserve_ids
):
    copied = model_copy(original, copy_references=True, preserve_ids=preserve_ids)
    assert copied.first is copied.second
    assert copied.first is not original.first
    assert (copied.id == original.id) is preserve_ids
    assert (copied.first.id == original.first.id) is preserve_ids
    for model in (copied, copied.first, copied.embedded):
        assert isinstance(model.model_fields_set, set)
        assert model.model_dump_doc()
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
    original.first.__pydantic_fields_set__.clear()
    original.embedded.__pydantic_fields_set__.clear()
    copied = model_copy(original, deep=False, preserve_ids=True)
    assert copied is not original
    assert copied.id == original.id
    assert original.first.model_fields_set == set()
    assert original.embedded.model_fields_set == set()
    assert copied.model_fields_set == original.model_fields_set


def test_deep_copy_does_not_restore_defaults_on_retained_references(original):
    del original.first.__dict__["description"]
    original.first.__pydantic_fields_set__.clear()
    source_values = original.first.__dict__.copy()
    copied = model_copy(original)
    assert copied.first is original.first
    assert original.first.__dict__ == source_values
    assert original.first.model_fields_set == set()


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


def test_copy_preserves_custom_primary_key_only_when_requested():
    from simstack.models.document import Field

    class CustomKey(Model):
        key: str = Field(primary_field=True)
        value: int

    original = CustomKey(key="identity", value=7)
    with pytest.raises(ValueError, match="preserve_ids=True"):
        model_copy(original)
    copied = model_copy(original, preserve_ids=True)
    assert copied is not original
    assert copied.key == original.key
    assert copied.model_dump_doc()["id"] == "identity"


def test_copy_does_not_silently_fill_missing_required_values(original):
    del original.__dict__["values"]
    with pytest.raises(ValueError, match="missing required field 'values'"):
        model_copy(original)


def test_published_file_copy_is_local_and_failed_publication_retains_version(monkeypatch):
    from datetime import datetime
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance
    from simstack.util.object_transfer import ObjectFileClient

    original = FileStack(name="input.txt", locations=[FileInstance(path="source-key", resource="__s3__",
        created_at=datetime.now(), location_type="s3", bucket="files", object_key="source-key")])
    before = original.model_dump()
    class Client:
        def copy_file(self, copied, source):
            assert copied.id != original.id and source == original.id
            raise RuntimeError("source disappeared during copy")
    monkeypatch.setattr(ObjectFileClient, "from_context", lambda: Client())
    copied = model_copy(original)
    assert copied._copy_source_file_id == original.id
    with pytest.raises(RuntimeError, match="source disappeared"):
        copied.ensure_uploaded()
    assert copied._copy_source_file_id == original.id
    assert original.model_dump() == before
    assert model_copy(original, preserve_ids=True).id == original.id


@pytest.mark.parametrize("bound", ["user", "runner", "local"])
@pytest.mark.parametrize("ambient", [False, True])
def test_published_file_copy_never_performs_implicit_api_writes(monkeypatch, bound, ambient):
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import Mock
    from simstack.core.context import context
    from simstack.models.files import FileStack
    from simstack.models.file_instance import FileInstance

    location = FileInstance(path="source-key", resource="__s3__", created_at=datetime.now(),
        location_type="s3", bucket="files", object_key="source-key")
    source = FileStack(name="input.txt", locations=[location])
    source_requests = SimpleNamespace(authorization=bound)
    current_requests = SimpleNamespace(authorization="local")
    object.__setattr__(source, "_object_requests", source_requests)
    monkeypatch.setattr(context, "_server", SimpleNamespace(requests=current_requests) if ambient else None)
    monkeypatch.setattr(ObjectFileClient, "from_context", _OBJECT_CLIENT_FROM_CONTEXT)
    selected = Mock(return_value=SimpleNamespace(copy_file=lambda *args: location.model_dump()))
    monkeypatch.setattr(ObjectFileClient, "for_api", selected)
    copied = model_copy(source)
    assert copied.id != source.id
    assert copied._copy_source_file_id == source.id
    assert copied._object_requests is source_requests
    selected.assert_not_called()
    assert source._object_requests is source_requests
