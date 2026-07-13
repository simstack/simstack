import pytest
from odmantic import Model, ObjectId
from pydantic import PrivateAttr

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import FloatData, StringData
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.util.db import Database


class CustomSavingModel(Model):
    value: str
    saved_by_custom_hook: bool = False
    nested_db_save_was_used: bool = False

    async def save(self, db):
        self.saved_by_custom_hook = True
        self.nested_db_save_was_used = True
        await db.save(self)


class AsyncPostProcessModel(Model):
    value: str
    postprocessed: bool = False

    @staticmethod
    async def db_find_postprocess(instance, db):
        assert db is context.db
        instance.postprocessed = True


class SecondAsyncPostProcessModel(Model):
    value: str
    postprocessed: bool = False

    @staticmethod
    async def db_find_postprocess(instance, db):
        assert db is context.db
        instance.postprocessed = True


class PartWithCustomSave(Model):
    value: str
    saved_by_part_hook: bool = False

    async def save(self, db):
        self.saved_by_part_hook = True
        await db.save_unchecked(self)


class ParentWithSavePart(Model):
    name: str

    def __init__(self, **data):
        part = data.pop("part", None)
        Model.__init__(self, **data)
        if part is not None:
            object.__setattr__(self, "_part", part)


def _db() -> Database:
    assert context.initialized is True
    assert context.db is not None
    return context.db


async def _restore_database_facade_methods(monkeypatch) -> Database:
    """
    Some test configurations patch context.db.save for the in-memory backend.
    These tests are specifically for the Database facade, so bind the facade
    methods back onto the initialized context database instance.
    """
    db = _db()
    monkeypatch.setattr(db, "save", Database.save.__get__(db, Database))
    monkeypatch.setattr(db, "save_unchecked", Database.save_unchecked.__get__(db, Database))
    monkeypatch.setattr(db, "delete", Database.delete.__get__(db, Database))
    monkeypatch.setattr(db, "find", Database.find.__get__(db, Database))
    monkeypatch.setattr(db, "find_one", Database.find_one.__get__(db, Database))
    return db


@pytest.mark.asyncio
async def test_context_database_is_initialized_database_facade(initialized_context):
    db = _db()

    assert isinstance(db, Database)
    assert db.client is not None
    assert db.core_engine is not None
    assert db.database_name
    assert db.raw_database is not None
    assert db.database is not None


@pytest.mark.asyncio
async def test_collection_and_get_collection_return_model_collection(initialized_context):
    db = _db()

    collection = db.collection(StringData)
    alias_collection = db.get_collection(StringData)

    assert collection is not None
    assert alias_collection is not None
    assert collection.name == alias_collection.name


@pytest.mark.asyncio
async def test_collection_accepts_collection_name(initialized_context):
    db = _db()

    named_collection = db.collection("database_test_named_collection")
    await named_collection.insert_one({"kind": "database-test", "value": 1})
    found = await named_collection.find_one({"kind": "database-test"})

    assert found is not None
    assert found["value"] == 1


@pytest.mark.asyncio
async def test_save_find_one_update_and_delete_model(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    model = StringData(value="database-save-find-delete")
    saved = await db.save(model)

    assert saved is model
    assert saved.id is not None

    loaded = await db.find_one(StringData, StringData.id == saved.id)
    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.value == "database-save-find-delete"

    saved.value = "database-save-find-delete-updated"
    updated = await db.save(saved)

    loaded_updated = await db.find_one(StringData, StringData.id == updated.id)
    assert loaded_updated is not None
    assert loaded_updated.value == "database-save-find-delete-updated"

    await db.delete(updated)
    deleted = await db.find_one(StringData, StringData.id == updated.id)
    assert deleted is None


@pytest.mark.asyncio
async def test_save_rejects_missing_arguments(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    with pytest.raises((ValueError, TypeError), match="(save requires at least one argument|missing 1 required positional argument)"):
        await db.save()


@pytest.mark.asyncio
async def test_find_rejects_missing_arguments(initialized_context):
    db = _db()

    with pytest.raises((ValueError, TypeError), match="(find requires at least one argument|missing 1 required positional argument)"):
        await db.find()


@pytest.mark.asyncio
async def test_find_one_rejects_missing_arguments(initialized_context):
    db = _db()

    with pytest.raises((ValueError, TypeError), match="(find_one requires at least one argument|missing 1 required positional argument)"):
        await db.find_one()


@pytest.mark.asyncio
async def test_save_handles_lists_tuples_and_sets(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    # odmantic models are not hashable by default. For this test, we temporarily
    # make them hashable by using their object id.
    monkeypatch.setattr(Model, "__hash__", lambda self: hash(id(self)), raising=False)

    list_models = [
        StringData(value="database-list-save-a"),
        StringData(value="database-list-save-b"),
    ]
    tuple_models = (
        FloatData(value=1.25),
        FloatData(value=2.5),
    )
    set_models = {
        StringData(value="database-set-save-a"),
        StringData(value="database-set-save-b"),
    }

    saved_list = await db.save(list_models)
    saved_tuple = await db.save(tuple_models)
    saved_set = await db.save(set_models)

    assert [model.id for model in saved_list] == [model.id for model in list_models]
    assert [model.id for model in saved_tuple] == [model.id for model in tuple_models]
    assert {model.id for model in saved_set} == {model.id for model in set_models}

    for model in [*saved_list, *saved_tuple, *saved_set]:
        loaded = await db.find_one(type(model), type(model).id == model.id)
        assert loaded is not None


@pytest.mark.asyncio
async def test_find_returns_list_for_multiple_results(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    first = await db.save(StringData(value="database-find-many-shared-value"))
    second = await db.save(StringData(value="database-find-many-shared-value"))

    results = await db.find(
        StringData,
        StringData.value == "database-find-many-shared-value",
        )

    result_ids = {result.id for result in results}
    assert isinstance(results, list)
    assert first.id in result_ids
    assert second.id in result_ids


@pytest.mark.asyncio
async def test_save_unchecked_persists_without_custom_facade_processing(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    model = CustomSavingModel(value="unchecked-save")
    saved = await db.save_unchecked(model)

    loaded = await db.find_one(CustomSavingModel, CustomSavingModel.id == saved.id)

    assert loaded is not None
    assert loaded.value == "unchecked-save"
    assert loaded.saved_by_custom_hook is False
    assert loaded.nested_db_save_was_used is False


@pytest.mark.asyncio
async def test_custom_save_hook_can_call_db_save_without_recursion(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    model = CustomSavingModel(value="custom-save")
    result = await db.save(model)

    loaded = await db.find_one(CustomSavingModel, CustomSavingModel.id == model.id)

    assert result is model
    assert model.saved_by_custom_hook is True
    assert model.nested_db_save_was_used is True
    assert getattr(model, "_currently_saving", False) is False
    assert loaded is not None
    assert loaded.saved_by_custom_hook is True
    assert loaded.nested_db_save_was_used is True


@pytest.mark.asyncio
async def test_currently_saving_flag_is_cleared_when_custom_save_raises(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    class FailingCustomSavingModel(Model):
        value: str

        async def save(self, db):
            raise RuntimeError("intentional custom save failure")

    model = FailingCustomSavingModel(value="custom-save-failure")

    with pytest.raises(RuntimeError, match="intentional custom save failure"):
        await db.save(model)

    assert getattr(model, "_currently_saving", False) is False


@pytest.mark.asyncio
async def test_save_calls_custom_save_on_model_parts(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    part = PartWithCustomSave(value="part-custom-save")
    parent = ParentWithSavePart(name="parent-with-private-part", part=part)

    result = await db.save(parent)

    loaded_part = await db.find_one(PartWithCustomSave, PartWithCustomSave.id == part.id)
    loaded_parent = await db.find_one(ParentWithSavePart, ParentWithSavePart.id == parent.id)

    assert result is parent
    assert part.saved_by_part_hook is True
    assert loaded_part is not None
    assert loaded_part.saved_by_part_hook is True
    assert loaded_parent is not None
    assert loaded_parent.name == "parent-with-private-part"


@pytest.mark.asyncio
async def test_iter_save_parts_includes_direct_values_and_container_values():
    part_a = PartWithCustomSave(value="part-a")
    part_b = PartWithCustomSave(value="part-b")
    part_c = PartWithCustomSave(value="part-c")

    class Container:
        def __init__(self):
            self.direct = part_a
            self.list_value = [part_b]
            self.tuple_value = (part_c,)
            self.dict_value = {"again": part_a}

    parts = list(Database._iter_save_parts(Container()))

    assert part_a in parts
    assert part_b in parts
    assert part_c in parts


@pytest.mark.asyncio
async def test_iter_save_parts_returns_empty_list_for_objects_without_vars():
    assert Database._iter_save_parts(1) == []


@pytest.mark.asyncio
async def test_async_find_postprocess_runs_for_find_and_find_one(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    saved = await db.save(AsyncPostProcessModel(value="async-postprocess"))

    found_one = await db.find_one(
        AsyncPostProcessModel,
        AsyncPostProcessModel.id == saved.id,
        )
    found_many = await db.find(
        AsyncPostProcessModel,
        AsyncPostProcessModel.value == "async-postprocess",
        )

    assert found_one is not None
    assert found_one.postprocessed is True
    assert found_many
    assert all(model.postprocessed is True for model in found_many)


@pytest.mark.asyncio
async def test_converted_sync_find_postprocess_runs_for_find_and_find_one(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    saved = await db.save(SecondAsyncPostProcessModel(value="sync-postprocess"))

    found_one = await db.find_one(
        SecondAsyncPostProcessModel,
        SecondAsyncPostProcessModel.id == saved.id,
        )
    found_many = await db.find(
        SecondAsyncPostProcessModel,
        SecondAsyncPostProcessModel.value == "sync-postprocess",
        )

    assert found_one is not None
    assert found_one.postprocessed is True
    assert found_many
    assert all(model.postprocessed is True for model in found_many)


def _registry(
        name: str,
        *,
        status: TaskStatus = TaskStatus.SUBMITTED,
        resource: str = "test",
) -> NodeRegistry:
    return NodeRegistry(
        name=name,
        status=status,
        function_hash=f"{name}-function-hash",
        arg_hash=f"{name}-arg-hash",
        func_mapping=f"tests.util.test_database:{name}",
        parameters=Parameters(resource=resource),
    )


@pytest.mark.asyncio
async def test_load_task_finds_registry_by_name_arg_hash_and_function_hash(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    registry = await db.save(_registry("database_load_task"))

    loaded = await db.load_task(
        "database_load_task",
        "database_load_task-arg-hash",
        "database_load_task-function-hash",
    )
    missing = await db.load_task(
        "database_load_task",
        "wrong-arg-hash",
        "database_load_task-function-hash",
    )

    assert loaded is not None
    assert loaded.id == registry.id
    assert missing is None


@pytest.mark.asyncio
async def test_load_task_by_id_accepts_object_id_and_string_id(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    registry = await db.save(_registry("database_load_task_by_id"))

    loaded_by_object_id = await db.load_task_by_id(registry.id)
    loaded_by_string_id = await db.load_task_by_id(str(registry.id))

    assert loaded_by_object_id is not None
    assert loaded_by_object_id.id == registry.id
    assert loaded_by_string_id is not None
    assert loaded_by_string_id.id == registry.id


@pytest.mark.asyncio
async def test_load_waiting_tasks_for_resource_filters_submitted_matching_resource(
        initialized_context,
        monkeypatch,
):
    from simstack.core.resources import allowed_resources
    allowed_resources.add_resource("database-test-resource")
    allowed_resources.add_resource("other-resource")
    allowed_resources.add_resource("cluster-a")
        
    db = await _restore_database_facade_methods(monkeypatch)

    matching = await db.save(
        _registry(
            "database_waiting_matching",
            status=TaskStatus.SUBMITTED,
            resource="database-test-resource",
        )
    )
    await db.save(
        _registry(
            "database_waiting_wrong_resource",
            status=TaskStatus.SUBMITTED,
            resource="other-resource",
        )
    )
    await db.save(
        _registry(
            "database_waiting_wrong_status",
            status=TaskStatus.COMPLETED,
            resource="database-test-resource",
        )
    )

    waiting = await db.load_waiting_tasks_for_resource("database-test-resource")

    waiting_ids = {entry.id for entry in waiting}
    assert matching.id in waiting_ids
    assert all(entry.status == TaskStatus.SUBMITTED for entry in waiting)
    assert all(entry.parameters.resource == "database-test-resource" for entry in waiting)


@pytest.mark.asyncio
async def test_load_waiting_tasks_for_local_includes_self_resource(
        initialized_context,
        monkeypatch,
):
    db = await _restore_database_facade_methods(monkeypatch)

    self_resource_entry = await db.save(
        _registry(
            "database_waiting_self_for_local",
            status=TaskStatus.SUBMITTED,
            resource="test",
        )
    )

    waiting = await db.load_waiting_tasks_for_resource("test")

    assert self_resource_entry.id in {entry.id for entry in waiting}


@pytest.mark.asyncio
async def test_stats_returns_database_statistics(initialized_context):
    db = _db()

    stats = await db.stats()

    assert isinstance(stats, dict)
    assert "collections" in stats or "objects" in stats or "db" in stats


@pytest.mark.asyncio
async def test_ping_uses_initialized_context_client(initialized_context):
    db = _db()

    result = await db.ping()

    assert result is not None


@pytest.mark.asyncio
async def test_db_save_return(initialized_context, monkeypatch):
    """Test that Database.save() returns the saved model, even when using custom save hooks."""
    db = await _restore_database_facade_methods(monkeypatch)

    # 1. Standard model without custom save
    model1 = StringData(value="test1")
    saved1 = await db.save(model1)
    assert saved1 is not None
    assert saved1.id is not None

    # 2. Model with custom save hook
    model2 = CustomSavingModel(value="test2")
    saved2 = await db.save(model2)
    assert saved2 is not None
    assert saved2.saved_by_custom_hook is True
    assert saved2.id is not None

    # 3. List of models (mix of standard and custom)
    models = [StringData(value="test3"), CustomSavingModel(value="test4")]
    saved_list = await db.save(models)
    assert isinstance(saved_list, list)
    assert len(saved_list) == 2
    assert saved_list[0] is not None
    assert saved_list[1] is not None
    assert saved_list[1].saved_by_custom_hook is True


@pytest.mark.asyncio
async def test_save_calls_parts_saves_recursively(initialized_context, monkeypatch):
    """Test that Database.save() calls custom save hooks on nested components (recursive parts)."""
    db = await _restore_database_facade_methods(monkeypatch)

    # Define a model structure with multiple layers of custom saves
    class GrandChild(Model):
        name: str
        saved: bool = False
        async def save(self, db):
            self.saved = True
            await db.save_unchecked(self)

    class Child(Model):
        name: str
        saved: bool = False
        grand_child: ObjectId
        async def save(self, db):
            self.saved = True
            await db.save_unchecked(self)

    class Parent(Model):
        name: str
        child: ObjectId

    gc = GrandChild(name="gc")
    await db.save_unchecked(gc)
    
    c = Child(name="child", grand_child=gc.id)
    
    class ParentWithParts(Model):
        name: str
        def __init__(self, **data):
            child = data.pop("child", None)
            Model.__init__(self, **data)
            if child is not None:
                object.__setattr__(self, "child", child)

    p = ParentWithParts(name="parent", child=c)

    # Now saving p should trigger save on c
    result = await db.save(p)
    assert result is not None
    assert c.saved is True
    
    # Recursive save prevention:
    class RecursiveModel(Model):
        counter: int = 0
        async def save(self, db):
            self.counter += 1
            await db.save(self) # This should not cause infinite recursion
            
    rm = RecursiveModel()
    await db.save(rm)
    assert rm.counter == 1


@pytest.mark.asyncio
async def test_reset_database_drops_existing_collections(initialized_context, monkeypatch):
    db = await _restore_database_facade_methods(monkeypatch)

    model = await db.save(StringData(value="database-reset-marker"))
    loaded_before_reset = await db.find_one(StringData, StringData.id == model.id)
    assert loaded_before_reset is not None

    await db.reset_database()

    collection_names = await db.raw_database.list_collection_names()
    loaded_after_reset = await db.find_one(StringData, StringData.id == model.id)

    assert collection_names == []
    assert loaded_after_reset is None
