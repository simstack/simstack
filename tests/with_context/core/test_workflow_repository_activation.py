from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.methods.workflow_repository import (
    _activation_task_is_active,
    activate_workflow_repo,
    validate_workflow_repo_candidate,
)
from simstack.models.models import ModelMapping, NodeModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.models.workflow_repository import WorkflowRepo, WorkflowRepoState


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, source: str, message: str) -> str:
    (repository / "workflow.py").write_text(source, encoding="utf-8")
    _git(repository, "add", "workflow.py")
    _git(
        repository,
        "-c",
        "user.name=SimStack",
        "-c",
        "user.email=simstack@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _archive(repository: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(repository.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(repository).as_posix())
    return output.getvalue()


def _repository(name: str, source_root: Path, source: str) -> WorkflowRepo:
    source_root.mkdir()
    _git(source_root, "init", "-b", "main")
    head_commit = _commit(source_root, source, "initial")
    archive = _archive(source_root)
    return WorkflowRepo(
        name=name,
        archive_bytes=archive,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        head_commit=head_commit,
    )


SOURCE_WITH_STALE_NODE = """from odmantic import Model
from simstack.core.node import node

class RepoResult(Model):
    value: int

@node(expose_in_submit=True)
def repo_entry(**kwargs) -> RepoResult:
    return RepoResult(value=1)

@node(expose_in_submit=False)
def stale_node(**kwargs) -> RepoResult:
    return RepoResult(value=0)
"""

SOURCE_REPLACEMENT = """from odmantic import Model
from simstack.core.node import node

class RepoResult(Model):
    value: int

@node(expose_in_submit=True)
def repo_entry(**kwargs) -> RepoResult:
    return RepoResult(value=2)
"""

SOURCE_CANDIDATE = """from odmantic import Model
from simstack.core.node import node
from simstack.core.simstack_result import SimstackResult

class RepoResult(Model):
    value: int

class CandidateResult(Model):
    value: int

@node(expose_in_submit=True)
def repo_entry(**kwargs) -> RepoResult:
    return RepoResult(value=2)

@node(expose_in_submit=False)
def candidate_entry(**kwargs) -> SimstackResult:
    \"\"\"Return candidate output.

    SimstackResult:
        result (CandidateResult): Candidate output.
    \"\"\"
    return SimstackResult()
"""

SOURCE_CANDIDATE_BLOCKER = """from odmantic import Model
from simstack.core.node import node

class CandidateResult(Model):
    value: int

@node(expose_in_submit=False)
def candidate_entry(**kwargs) -> CandidateResult:
    return CandidateResult(value=4)
"""

SOURCE_FOR_LOCK_TEST = """from odmantic import Model
from simstack.core.node import node

class LockResult(Model):
    value: int

@node(expose_in_submit=True)
def lock_entry(**kwargs) -> LockResult:
    return LockResult(value=1)
"""

SOURCE_WITH_INVALID_INPUT = """from odmantic import Model
from simstack.core.node import node

class InvalidResult(Model):
    value: int

class MissingInput:
    pass

@node(expose_in_submit=False)
def invalid_entry(value: MissingInput, **kwargs) -> InvalidResult:
    return InvalidResult(value=1)
"""


async def _candidate_validation_task(
    target: WorkflowRepo,
    archive: bytes,
    head_commit: str,
) -> NodeRegistry:
    task = await context.db.save(
        NodeRegistry(
            name="validate_workflow_repo_candidate",
            status=TaskStatus.RUNNING,
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping=(
                "simstack.methods.workflow_repository."
                "validate_workflow_repo_candidate"
            ),
            parameters=Parameters(force_rerun=True),
        )
    )
    await context.db.get_collection(NodeRegistry).update_one(
        {"_id": task.id},
        {
            "$set": {
                "workflow_repo_candidate": {
                    "target_repo_id": target.id,
                    "expected_head": target.head_commit,
                    "expected_archive_sha256": target.archive_sha256,
                    "archive_bytes": archive,
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "head_commit": head_commit,
                }
            }
        },
    )
    return task


async def _registration_snapshot():
    snapshots = []
    for model in (NodeModel, ModelMapping):
        cursor = context.db.get_collection(model).find({})
        documents = await cursor.to_list(length=None)
        snapshots.append(
            sorted(
                (deepcopy(document) for document in documents),
                key=lambda document: str(document["_id"]),
            )
        )
    return snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING],
)
async def test_workflow_validation_keeps_slurm_tasks_active(
    initialized_context,
    status: TaskStatus,
):
    task = await context.db.save(
        NodeRegistry(
            name="slurm_workflow_validation",
            status=status,
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping="simstack.methods.workflow_repository.activate_workflow_repo",
            parameters=Parameters(force_rerun=True),
        )
    )

    assert await _activation_task_is_active(context.db, task.id) is True

    await context.db.delete(task)


@pytest.mark.asyncio
async def test_same_repository_replaces_owned_registrations_and_cross_repo_conflicts(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    owner_root = tmp_path / "owner"
    owner = await context.db.save(
        _repository("owner", owner_root, SOURCE_WITH_STALE_NODE)
    )

    assert await activate_workflow_repo._inner(owner) is True
    owner_entry = await context.db.find_one(NodeModel, NodeModel.name == "repo_entry")
    assert owner_entry is not None
    assert owner_entry.code_source is not None
    assert owner_entry.code_source.repo_id == owner.id

    replacement_head = _commit(owner_root, SOURCE_REPLACEMENT, "replace")
    replacement_archive = _archive(owner_root)
    owner.archive_bytes = replacement_archive
    owner.archive_sha256 = hashlib.sha256(replacement_archive).hexdigest()
    owner.head_commit = replacement_head
    await context.db.save(owner)

    assert await activate_workflow_repo._inner(owner) is True
    assert await context.db.find_one(NodeModel, NodeModel.name == "stale_node") is None
    replacement_entry = await context.db.find_one(
        NodeModel, NodeModel.name == "repo_entry"
    )
    assert replacement_entry is not None
    assert replacement_entry.code_source is not None
    assert replacement_entry.code_source.commit == replacement_head

    contender = await context.db.save(
        _repository("contender", tmp_path / "contender", SOURCE_REPLACEMENT)
    )
    assert await activate_workflow_repo._inner(contender) is False
    contender = await context.db.find_one(
        WorkflowRepo, WorkflowRepo.id == contender.id
    )
    assert contender is not None
    assert contender.state == WorkflowRepoState.FAILED
    assert contender.last_error is not None
    assert "registration conflicts" in contender.last_error

    preserved_entry = await context.db.find_one(
        NodeModel, NodeModel.name == "repo_entry"
    )
    preserved_model = await context.db.find_one(
        ModelMapping, ModelMapping.name == "RepoResult"
    )
    assert preserved_entry is not None and preserved_entry.code_source is not None
    assert preserved_model is not None and preserved_model.code_source is not None
    assert preserved_entry.code_source.repo_id == owner.id
    assert preserved_model.code_source.repo_id == owner.id

    for registration_type in (NodeModel, ModelMapping):
        for registration in await context.db.find(registration_type):
            if registration.code_source is not None:
                await context.db.delete(registration)
    for repository in await context.db.find(WorkflowRepo):
        await context.db.delete(repository)
    await context.refresh_mappings()


@pytest.mark.asyncio
async def test_candidate_validation_uses_activation_checks_without_persisting_changes(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    target_root = tmp_path / "target"
    target = await context.db.save(
        _repository("candidate-target", target_root, SOURCE_WITH_STALE_NODE)
    )
    assert await activate_workflow_repo._inner(target) is True
    target = await context.db.find_one(
        WorkflowRepo,
        WorkflowRepo.id == target.id,
    )
    assert target is not None

    candidate_head = _commit(target_root, SOURCE_CANDIDATE, "candidate")
    candidate_archive = _archive(target_root)
    success_task = await _candidate_validation_task(
        target,
        candidate_archive,
        candidate_head,
    )

    target_before = deepcopy(
        await context.db.get_collection(WorkflowRepo).find_one({"_id": target.id})
    )
    registrations_before = await _registration_snapshot()
    original_save = context.db.save
    original_delete = context.db.delete

    async def reject_live_registration_save(instance, *args, **kwargs):
        if isinstance(instance, (NodeModel, ModelMapping)):
            raise AssertionError("candidate validation wrote a live registration")
        return await original_save(instance, *args, **kwargs)

    async def reject_live_registration_delete(instance, *args, **kwargs):
        if isinstance(instance, (NodeModel, ModelMapping)):
            raise AssertionError("candidate validation deleted a live registration")
        return await original_delete(instance, *args, **kwargs)

    monkeypatch.setattr(context.db, "save", reject_live_registration_save)
    monkeypatch.setattr(context.db, "delete", reject_live_registration_delete)
    assert (
        await validate_workflow_repo_candidate._inner(task_id=success_task.id)
        is True
    )
    assert await context.db.get_collection(WorkflowRepo).find_one(
        {"_id": target.id}
    ) == target_before
    assert await _registration_snapshot() == registrations_before

    monkeypatch.setattr(context.db, "save", original_save)
    monkeypatch.setattr(context.db, "delete", original_delete)
    blocker = await context.db.save(
        _repository(
            "candidate-blocker",
            tmp_path / "blocker",
            SOURCE_CANDIDATE_BLOCKER,
        )
    )
    assert await activate_workflow_repo._inner(blocker) is True
    conflict_task = await _candidate_validation_task(
        target,
        candidate_archive,
        candidate_head,
    )
    target_before_conflict = deepcopy(
        await context.db.get_collection(WorkflowRepo).find_one({"_id": target.id})
    )
    registrations_before_conflict = await _registration_snapshot()
    monkeypatch.setattr(context.db, "save", reject_live_registration_save)
    monkeypatch.setattr(context.db, "delete", reject_live_registration_delete)
    with pytest.raises(ValueError) as raised:
        await validate_workflow_repo_candidate._inner(task_id=conflict_task.id)
    assert "registration conflicts" in str(raised.value)
    assert "candidate_entry" in str(raised.value)
    assert "CandidateResult" in str(raised.value)
    assert await context.db.get_collection(WorkflowRepo).find_one(
        {"_id": target.id}
    ) == target_before_conflict
    assert await _registration_snapshot() == registrations_before_conflict

    monkeypatch.setattr(context.db, "save", original_save)
    monkeypatch.setattr(context.db, "delete", original_delete)
    for task in (success_task, conflict_task):
        await context.db.delete(task)
    for registration_type in (NodeModel, ModelMapping):
        for registration in await context.db.find(registration_type):
            if registration.code_source is not None:
                await context.db.delete(registration)
    for repository in await context.db.find(WorkflowRepo):
        await context.db.delete(repository)
    await context.refresh_mappings()


@pytest.mark.asyncio
async def test_activation_validates_tables_before_writing_live_registrations(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    repository = await context.db.save(
        _repository(
            "invalid-input",
            tmp_path / "invalid-input",
            SOURCE_WITH_INVALID_INPUT,
        )
    )
    registrations_before = await _registration_snapshot()
    original_save = context.db.save
    original_delete = context.db.delete

    async def reject_live_registration_save(instance, *args, **kwargs):
        if isinstance(instance, (NodeModel, ModelMapping)):
            raise AssertionError("activation wrote before table validation completed")
        return await original_save(instance, *args, **kwargs)

    async def reject_live_registration_delete(instance, *args, **kwargs):
        if isinstance(instance, (NodeModel, ModelMapping)):
            raise AssertionError("activation deleted before table validation completed")
        return await original_delete(instance, *args, **kwargs)

    monkeypatch.setattr(context.db, "save", reject_live_registration_save)
    monkeypatch.setattr(context.db, "delete", reject_live_registration_delete)
    assert await activate_workflow_repo._inner(repository) is False
    assert await _registration_snapshot() == registrations_before

    failed_repository = await context.db.find_one(
        WorkflowRepo,
        WorkflowRepo.id == repository.id,
    )
    assert failed_repository is not None
    assert failed_repository.state == WorkflowRepoState.FAILED
    assert failed_repository.last_error is not None
    assert "unregistered models" in failed_repository.last_error
    assert "workflow.MissingInput" in failed_repository.last_error

    monkeypatch.setattr(context.db, "save", original_save)
    monkeypatch.setattr(context.db, "delete", original_delete)
    await context.db.delete(failed_repository)


@pytest.mark.asyncio
async def test_activation_does_not_steal_an_expired_lock(
    initialized_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SIMSTACK_WORKFLOW_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "simstack.methods.workflow_repository._ACTIVATION_LOCK_ATTEMPTS",
        1,
    )
    repository = await context.db.save(
        _repository("locked", tmp_path / "locked", SOURCE_FOR_LOCK_TEST)
    )
    lock_collection = context.db.get_collection("workflow_repo_activation_lock")
    await lock_collection.update_one(
        {"_id": "registrations"},
        {
            "$set": {
                "token": "still-owned",
                "repo_id": "another-repository",
                "lease_expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
            }
        },
        upsert=True,
    )

    assert await activate_workflow_repo._inner(repository) is False
    lock = await lock_collection.find_one({"_id": "registrations"})
    assert lock is not None
    assert lock["token"] == "still-owned"
    assert await context.db.find_one(NodeModel, NodeModel.name == "lock_entry") is None

    await lock_collection.update_one(
        {"_id": "registrations", "token": "still-owned"},
        {"$set": {"token": None}, "$unset": {"lease_expires_at": ""}},
    )
    await context.db.delete(repository)
