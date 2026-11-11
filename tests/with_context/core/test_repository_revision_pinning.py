"""Pinned child/cache decisions belong to Server, exercised through HTTP."""
from uuid import uuid4

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import Node, compute_arg_hash
from simstack.models import NodeModel, NodeRegistry, Parameters
from simstack.runner_runtime.launch_claims import task_execution


def pinned_cache(**kwargs):
    return True


async def seed_revision(domain_bridge, revision, version):
    from server.mongo_repository import MongoRepository
    from server.storage import Database
    await domain_bridge.raw.repository_versions.insert_one({"_id": revision, "status": "ready", "version": version,
        "runner_protocol": 3})
    repository = MongoRepository(client=domain_bridge.database.client,
        database=domain_bridge.database.database_name, repository_revision=revision,
        user=domain_bridge.auth.user)
    storage = Database(engine=repository)
    await storage.save(NodeModel(name="pinned_cache", function_mapping=f"{__name__}.pinned_cache",
        input_mappings=[], default_parameters=Parameters(), repository_revision=revision,
        repository_version=version))


async def seed_parent(server_storage, domain_bridge, revision, version):
    parent = NodeRegistry(name="parent", status=TaskStatus.RUNNING,
        func_mapping="test.parent", function_hash="", arg_hash="parent",
        parameters=Parameters(resource="test"), repository_revision=revision,
        repository_version=version)
    await server_storage.save(parent)
    from bson import ObjectId
    await domain_bridge.raw.node_registry.update_one({"_id": ObjectId(str(parent.id))}, {"$set": {
        "_runner_generation": str(uuid4()), "_runner_claim_id": str(uuid4()),
        "_runner_start_id": str(uuid4()), "_runner_resource": "test",
    }})
    return await context.server.tasks.get_task(parent.id)


@pytest.mark.asyncio
async def test_completed_results_are_reused_only_within_same_repository_revision(server_storage, domain_bridge):
    revisions = ["a" * 64, "b" * 64, "c" * 64]
    for index, revision in enumerate(revisions, 1):
        await seed_revision(domain_bridge, revision, index)
    latest = NodeModel(name="pinned_cache", function_mapping=f"{__name__}.pinned_cache",
        input_mappings=[], default_parameters=Parameters(), repository_revision=revisions[-1], repository_version=3)
    await server_storage.save(latest)
    await context.refresh_mappings()
    cached = []
    for revision in revisions[:2]:
        task = NodeRegistry(name="pinned_cache", status=TaskStatus.COMPLETED,
            func_mapping=latest.function_mapping, function_hash="", arg_hash=compute_arg_hash([]),
            parameters=Parameters(resource="test"), repository_revision=revision)
        await server_storage.save(task)
        cached.append(task)
    for index, revision in enumerate(revisions, 1):
        parent = await seed_parent(server_storage, domain_bridge, revision, index)
        invocation = Node(func=pinned_cache, is_async=False, parent_id=parent.id, parameters=Parameters())
        try:
            with task_execution(parent):
                await invocation.get_node_registry()
            if index < 3:
                assert invocation.id == cached[index - 1].id
            else:
                assert invocation.id not in {task.id for task in cached}
            assert invocation.registry_entry.repository_revision == revision
        finally:
            reservation = getattr(invocation, "_inline_launch_reservation", None)
            if reservation is not None:
                reservation.close()


@pytest.mark.asyncio
async def test_nested_node_inherits_parent_pin_after_latest_catalogue_advances(server_storage, domain_bridge):
    old_revision, latest_revision = "a" * 64, "b" * 64
    await seed_revision(domain_bridge, old_revision, 1)
    await seed_revision(domain_bridge, latest_revision, 2)
    await server_storage.save(NodeModel(name="pinned_cache", function_mapping=f"{__name__}.pinned_cache",
        input_mappings=[], default_parameters=Parameters(), repository_revision=latest_revision, repository_version=2))
    await context.refresh_mappings()
    parent = await seed_parent(server_storage, domain_bridge, old_revision, 1)
    invocation = Node(func=pinned_cache, is_async=False, parent_id=parent.id, parameters=Parameters())
    try:
        with task_execution(parent):
            await invocation.get_node_registry()
        assert invocation.registry_entry.repository_revision == old_revision
        assert invocation.registry_entry.repository_version == 1
        commands = [body for method, path, body in domain_bridge.api.requests.transport.calls
                    if method == "POST" and path.endswith("/children")]
        assert commands[-1]["repository_revision"] is None
    finally:
        reservation = getattr(invocation, "_inline_launch_reservation", None)
        if reservation is not None:
            reservation.close()
