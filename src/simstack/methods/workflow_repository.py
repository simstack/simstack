from __future__ import annotations

import asyncio
import hashlib
import inspect
import importlib
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from bson import ObjectId as BSONObjectId
from odmantic import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from simstack.core.definitions import TaskStatus
from simstack.core.node import node
from simstack.core.workflow_repository import (
    MAX_WORKFLOW_ARCHIVE_BYTES,
    annotate_workflow_module,
    cached_repository_checkout,
    cached_repository_model_checkout,
    prepare_repository_import,
)
from simstack.models.models import ModelMapping, NodeModel
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters
from simstack.models.simstack_model import is_simstack_model
from simstack.models.workflow_repository import (
    CodeSource,
    WorkflowRepo,
    WorkflowRepoState,
)
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import is_node_function, make_node_table


logger = logging.getLogger("WorkflowRepositoryActivation")
_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ipynb_checkpoints",
}
_ACTIVATION_LOCK_ATTEMPTS = 240
_CANDIDATE_FIELDS = {
    "target_repo_id",
    "expected_head",
    "expected_archive_sha256",
    "archive_bytes",
    "archive_sha256",
    "head_commit",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _candidate_payload(
    task_document: Mapping[str, Any],
) -> tuple[ObjectId, str, str, WorkflowRepo]:
    payload = task_document.get("workflow_repo_candidate")
    if not isinstance(payload, Mapping) or set(payload) != _CANDIDATE_FIELDS:
        raise ValueError("workflow repository candidate payload is missing or invalid")

    raw_target_repo_id = payload["target_repo_id"]
    if not isinstance(raw_target_repo_id, BSONObjectId):
        raise ValueError("workflow repository candidate target_repo_id is invalid")
    target_repo_id = ObjectId(str(raw_target_repo_id))
    expected_head = payload["expected_head"]
    expected_archive_sha256 = payload["expected_archive_sha256"]
    archive_sha256 = payload["archive_sha256"]
    head_commit = payload["head_commit"]
    if not all(
        isinstance(value, str)
        for value in (
            expected_head,
            expected_archive_sha256,
            archive_sha256,
            head_commit,
        )
    ):
        raise ValueError("workflow repository candidate hashes and heads must be strings")
    expected_head = CodeSource(repo_id=target_repo_id, commit=expected_head).commit
    if len(expected_archive_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in expected_archive_sha256
    ):
        raise ValueError("workflow repository candidate expected archive SHA-256 is invalid")

    archive_value = payload["archive_bytes"]
    if not isinstance(archive_value, bytes):
        raise ValueError("workflow repository candidate archive must be BSON binary data")
    archive_bytes = bytes(archive_value)
    if len(archive_bytes) > MAX_WORKFLOW_ARCHIVE_BYTES:
        raise ValueError("workflow repository candidate archive exceeds the 12 MB limit")
    if hashlib.sha256(archive_bytes).hexdigest() != archive_sha256:
        raise ValueError("workflow repository candidate archive SHA-256 mismatch")

    candidate = WorkflowRepo(
        id=target_repo_id,
        name="workflow-validation-candidate",
        archive_bytes=archive_bytes,
        archive_sha256=archive_sha256,
        head_commit=head_commit,
    )
    return target_repo_id, expected_head, expected_archive_sha256, candidate


def _module_mapping(repository_root: Path, file_path: Path) -> str:
    relative_path = file_path.relative_to(repository_root).with_suffix("")
    if relative_path.name == "__init__":
        return ".".join(relative_path.parts[:-1])
    return ".".join(relative_path.parts)


def _is_registered_model(model_class: type[Any]) -> bool:
    base_names = {base.__name__ for base in model_class.__bases__}
    return (
        "Model" in base_names
        or "EmbeddedModel" in base_names
        or any("UIModel" in name for name in base_names)
        or is_simstack_model(model_class)
    )


def _discover_repository_registrations(
    repository_root: Path,
    code_source: CodeSource,
) -> tuple[dict[str, str], dict[str, str]]:
    nodes: dict[str, str] = {}
    models: dict[str, str] = {}
    conflicts: list[str] = []
    import_failures: list[str] = []

    python_files = sorted(
        path
        for path in repository_root.rglob("*.py")
        if path.is_file()
        and path.name != "__init__.py"
        and not any(part in _EXCLUDED_PARTS for part in path.parts)
    )
    for file_path in python_files:
        module_path = _module_mapping(repository_root, file_path)
        prepare_repository_import(repository_root, module_path)
        try:
            module = importlib.import_module(module_path)
            module_file = getattr(module, "__file__", None)
            if (
                module_file is None
                or Path(module_file).resolve() != file_path.resolve()
            ):
                raise ImportError(
                    f"module '{module_path}' loaded from an unexpected file"
                )
        except BaseException as exc:
            import_failures.append(
                f"{file_path.relative_to(repository_root)}: "
                f"{exc.__class__.__name__}: {exc}"
            )
            continue
        if not isinstance(module, ModuleType):
            import_failures.append(
                f"{file_path.relative_to(repository_root)}: module import returned no module"
            )
            continue
        annotate_workflow_module(module, code_source)

        for function_name, function in inspect.getmembers(module, inspect.isfunction):
            if function.__module__ != module.__name__ or not is_node_function(function):
                continue
            node_name = getattr(function, "_node_name", function_name)
            mapping = f"{module.__name__}.{function_name}"
            previous = nodes.get(node_name)
            if previous is not None and previous != mapping:
                conflicts.append(
                    f"node name '{node_name}' is declared by both '{previous}' and '{mapping}'"
                )
            nodes[node_name] = mapping

        for class_name, model_class in inspect.getmembers(module, inspect.isclass):
            if model_class.__module__ != module.__name__ or not _is_registered_model(
                model_class
            ):
                continue
            mapping = f"{module.__name__}.{class_name}"
            previous = models.get(class_name)
            if previous is not None and previous != mapping:
                conflicts.append(
                    f"model name '{class_name}' is declared by both '{previous}' and '{mapping}'"
                )
            models[class_name] = mapping

    if import_failures:
        raise ValueError(
            "repository import validation failed: " + "; ".join(import_failures[:20])
        )
    if conflicts:
        raise ValueError("repository registration conflicts: " + "; ".join(conflicts))
    if not nodes and not models:
        raise ValueError("repository contains no importable SimStack nodes or models")
    return nodes, models


def _owned_by_repo(registration: NodeModel | ModelMapping, repo_id: Any) -> bool:
    return (
        registration.code_source is not None
        and registration.code_source.repo_id == repo_id
    )


class _RegistrationValidationDatabase:
    """Build a candidate catalog without writing registration collections."""

    def __init__(
        self,
        database: Any,
        candidate: WorkflowRepo,
        nodes: list[NodeModel],
        models: list[ModelMapping],
    ) -> None:
        self._database = database
        self._candidate = candidate
        self._registrations: dict[type[Any], list[NodeModel | ModelMapping]] = {
            NodeModel: [node.model_copy(deep=True) for node in nodes],
            ModelMapping: [model.model_copy(deep=True) for model in models],
        }

    @staticmethod
    def _matches(
        instance: Any,
        queries: tuple[Any, ...],
    ) -> bool:
        for query in queries:
            for field_name, predicate in dict(query).items():
                if not isinstance(predicate, Mapping) or set(predicate) != {"$eq"}:
                    raise ValueError(
                        "candidate registration validation only supports equality queries"
                    )
                model_field_name = "id" if field_name == "_id" else field_name
                if getattr(instance, model_field_name) != predicate["$eq"]:
                    return False
        return True

    async def find(self, model: type[Any], *queries: Any, **kwargs: Any) -> Any:
        registrations = self._registrations.get(model)
        if registrations is None:
            return await self._database.find(model, *queries, **kwargs)
        if kwargs:
            raise ValueError(
                "candidate registration validation does not support query options"
            )
        return [
            registration
            for registration in registrations
            if self._matches(registration, queries)
        ]

    async def find_one(
        self,
        model: type[Any],
        *queries: Any,
        **kwargs: Any,
    ) -> Any:
        registrations = self._registrations.get(model)
        if registrations is not None:
            if kwargs:
                raise ValueError(
                    "candidate registration validation does not support query options"
                )
            return next(
                (
                    registration
                    for registration in registrations
                    if self._matches(registration, queries)
                ),
                None,
            )
        if model is WorkflowRepo and self._matches(self._candidate, queries):
            return self._candidate
        return await self._database.find_one(model, *queries, **kwargs)

    async def save(
        self,
        registration: NodeModel | ModelMapping,
        *args: Any,
        **kwargs: Any,
    ) -> NodeModel | ModelMapping:
        registrations = self._registrations.get(type(registration))
        if registrations is None:
            return await self._database.save(registration, *args, **kwargs)
        if args or kwargs:
            raise ValueError(
                "candidate registration validation does not support save options"
            )

        unique_fields = (
            ("name", "function_mapping")
            if isinstance(registration, NodeModel)
            else ("name", "mapping")
        )
        for existing in registrations:
            if existing.id == registration.id:
                continue
            for field_name in unique_fields:
                if getattr(existing, field_name) == getattr(registration, field_name):
                    raise ValueError(
                        f"duplicate candidate registration {field_name} "
                        f"'{getattr(registration, field_name)}'"
                    )
        registrations[:] = [
            existing
            for existing in registrations
            if existing.id != registration.id
        ]
        registrations.append(registration.model_copy(deep=True))
        return registration

    async def delete(
        self,
        registration: NodeModel | ModelMapping,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        registrations = self._registrations.get(type(registration))
        if registrations is None:
            await self._database.delete(registration, *args, **kwargs)
            return
        if args or kwargs:
            raise ValueError(
                "candidate registration validation does not support delete options"
            )
        registrations[:] = [
            existing
            for existing in registrations
            if existing.id != registration.id
        ]


async def _registration_conflicts(
    db: Any,
    replaceable_repo_id: ObjectId,
    node_candidates: dict[str, str],
    model_candidates: dict[str, str],
) -> list[str]:
    conflicts: list[str] = []
    node_names = set(node_candidates)
    node_mappings = set(node_candidates.values())
    model_names = set(model_candidates)
    model_mappings = set(model_candidates.values())

    for registration in await db.find(NodeModel):
        if _owned_by_repo(registration, replaceable_repo_id):
            continue
        if (
            registration.name in node_names
            or registration.function_mapping in node_mappings
        ):
            owner = (
                f"repository {registration.code_source.repo_id}"
                if registration.code_source is not None
                else "unowned or built-in code"
            )
            conflicts.append(
                f"node '{registration.name}' ({registration.function_mapping}) is owned by {owner}"
            )

    for registration in await db.find(ModelMapping):
        if _owned_by_repo(registration, replaceable_repo_id):
            continue
        if registration.name in model_names or registration.mapping in model_mappings:
            owner = (
                f"repository {registration.code_source.repo_id}"
                if registration.code_source is not None
                else "unowned or built-in code"
            )
            conflicts.append(
                f"model '{registration.name}' ({registration.mapping}) is owned by {owner}"
            )
    return sorted(set(conflicts))


async def _save_repository_state(
    db: Any,
    repository: WorkflowRepo,
    state: WorkflowRepoState,
    *,
    error: str | None = None,
) -> bool:
    updated_at = _utc_now()
    result = await db.get_collection(WorkflowRepo).update_one(
        {
            "_id": repository.id,
            "head_commit": repository.head_commit,
            "archive_sha256": repository.archive_sha256,
        },
        {
            "$set": {
                "state": state.value,
                "last_error": error,
                "updated_at": updated_at,
            }
        },
    )
    if result.matched_count != 1:
        return False
    repository.state = state
    repository.last_error = error
    repository.updated_at = updated_at
    return True


async def _activation_task_is_active(db: Any, task_id: Any) -> bool:
    if task_id is None:
        return True
    task = await db.find_one(NodeRegistry, NodeRegistry.id == task_id)
    return task is not None and task.status in {
        TaskStatus.RETRIEVED,
        TaskStatus.RUNNING,
        TaskStatus.SLURM_QUEUED,
        TaskStatus.SLURM_RUNNING,
    }


async def _acquire_registration_lock(
    db: Any,
    repo_id: ObjectId,
    task_id: Any,
) -> tuple[Any, str]:
    activation_lock = db.get_collection("workflow_repo_activation_lock")
    token = uuid.uuid4().hex
    for _ in range(_ACTIVATION_LOCK_ATTEMPTS):
        try:
            lock = await activation_lock.find_one_and_update(
                {
                    "_id": "registrations",
                    "$or": [
                        {"token": None},
                        {"token": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "token": token,
                        "repo_id": repo_id,
                        "task_id": task_id,
                        "acquired_at": _utc_now(),
                    },
                    "$inc": {"epoch": 1},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            lock = None
        if lock is not None and lock.get("token") == token:
            return activation_lock, token
        await asyncio.sleep(0.5)
    raise RuntimeError(
        "Repository activation is busy; retry validation in a few minutes"
    )


async def _release_registration_lock(
    activation_lock: Any,
    token: str,
    repo_id: ObjectId,
) -> None:
    try:
        await activation_lock.update_one(
            {"_id": "registrations", "token": token},
            {
                "$set": {"token": None},
                "$unset": {
                    "repo_id": "",
                    "task_id": "",
                    "acquired_at": "",
                    "lease_expires_at": "",
                },
            },
        )
    except Exception:
        logger.exception(
            "Workflow repository activation lock release failed",
            extra={"repo_id": str(repo_id)},
        )


async def _owned_registration_snapshot(
    db: Any,
    repo_id: ObjectId,
) -> tuple[list[NodeModel], list[ModelMapping]]:
    return (
        [
            registration.model_copy(deep=True)
            for registration in await db.find(NodeModel)
            if _owned_by_repo(registration, repo_id)
        ],
        [
            registration.model_copy(deep=True)
            for registration in await db.find(ModelMapping)
            if _owned_by_repo(registration, repo_id)
        ],
    )


async def _build_registration_tables(
    db: Any,
    repo_id: ObjectId,
    checkout: Path,
    node_candidates: dict[str, str],
    model_candidates: dict[str, str],
    previous_nodes: list[NodeModel],
    previous_models: list[ModelMapping],
) -> tuple[dict[str, str], dict[str, str]]:
    for registration in (*previous_nodes, *previous_models):
        await db.delete(registration)

    await make_model_table(
        db,
        dirs=[checkout],
        clear=False,
        project_root=checkout,
        ignore_entrypoints=True,
        refresh_mappings=not isinstance(db, _RegistrationValidationDatabase),
    )
    await make_node_table(
        db,
        dirs=[checkout],
        clear=False,
        project_root=checkout,
        ignore_entrypoints=True,
        refresh_mappings=not isinstance(db, _RegistrationValidationDatabase),
    )

    registered_nodes = {
        registration.name: registration.function_mapping
        for registration in await db.find(NodeModel)
        if _owned_by_repo(registration, repo_id)
    }
    registered_models = {
        registration.name: registration.mapping
        for registration in await db.find(ModelMapping)
        if _owned_by_repo(registration, repo_id)
    }
    missing = [
        f"node '{name}' ({mapping})"
        for name, mapping in node_candidates.items()
        if registered_nodes.get(name) != mapping
    ] + [
        f"model '{name}' ({mapping})"
        for name, mapping in model_candidates.items()
        if registered_models.get(name) != mapping
    ]
    if missing:
        raise ValueError(
            "repository validation did not register: " + "; ".join(missing)
        )
    missing_inputs = [
        f"{node.name}.{input_mapping.name} -> {input_mapping.mapping}"
        for node in await db.find(NodeModel)
        if _owned_by_repo(node, repo_id)
        for input_mapping in node.input_mappings
        if await db.find_one(
            ModelMapping, ModelMapping.mapping == input_mapping.mapping
        )
        is None
    ]
    if missing_inputs:
        raise ValueError(
            "repository node inputs reference unregistered models: "
            + "; ".join(sorted(missing_inputs))
        )
    return registered_nodes, registered_models


async def _validate_registration_tables_without_writes(
    database: Any,
    repository: WorkflowRepo,
    checkout: Path,
    node_candidates: dict[str, str],
    model_candidates: dict[str, str],
) -> tuple[
    dict[str, str],
    dict[str, str],
    list[NodeModel],
    list[ModelMapping],
]:
    live_nodes = list(await database.find(NodeModel))
    live_models = list(await database.find(ModelMapping))
    previous_nodes = [
        registration.model_copy(deep=True)
        for registration in live_nodes
        if _owned_by_repo(registration, repository.id)
    ]
    previous_models = [
        registration.model_copy(deep=True)
        for registration in live_models
        if _owned_by_repo(registration, repository.id)
    ]
    validation_database = _RegistrationValidationDatabase(
        database,
        repository,
        live_nodes,
        live_models,
    )
    registered_nodes, registered_models = await _build_registration_tables(
        validation_database,
        repository.id,
        checkout,
        node_candidates,
        model_candidates,
        previous_nodes,
        previous_models,
    )
    return registered_nodes, registered_models, previous_nodes, previous_models


async def _restore_registration_snapshot(
    db: Any,
    repo_id: ObjectId,
    previous_nodes: list[NodeModel],
    previous_models: list[ModelMapping],
) -> None:
    for registration in await db.find(NodeModel):
        if _owned_by_repo(registration, repo_id):
            await db.delete(registration)
    for registration in await db.find(ModelMapping):
        if _owned_by_repo(registration, repo_id):
            await db.delete(registration)
    for registration in (*previous_models, *previous_nodes):
        await db.save(registration)


@node(parameters=Parameters(), expose_in_submit=False)
async def validate_workflow_repo_candidate(**kwargs: Any) -> bool:
    """Validate a staged replacement without changing its target repository."""

    from simstack.core.context import context

    database = context.db
    raw_task_id = kwargs.get("task_id")
    if not isinstance(raw_task_id, BSONObjectId):
        raise ValueError("workflow repository candidate validation task id is invalid")
    task_id = ObjectId(str(raw_task_id))
    task_collection = database.get_collection(NodeRegistry)
    task_document = await task_collection.find_one({"_id": raw_task_id})
    if task_document is None or not await _activation_task_is_active(
        database, task_id
    ):
        raise RuntimeError("workflow repository candidate validation task is not active")

    target_repo_id, expected_head, expected_archive_sha256, candidate = (
        _candidate_payload(task_document)
    )
    target = await database.find_one(
        WorkflowRepo,
        WorkflowRepo.id == target_repo_id,
    )
    if target is None or (
        target.head_commit != expected_head
        or target.archive_sha256 != expected_archive_sha256
    ):
        raise ValueError("workflow repository changed before candidate validation")

    logger.info(
        "Validating workflow repository candidate",
        extra={
            "repo_id": str(target_repo_id),
            "expected_head": expected_head,
            "candidate_head": candidate.head_commit,
            "candidate_archive_sha256": candidate.archive_sha256,
            "task_id": str(task_id),
        },
    )
    activation_lock: Any | None = None
    activation_lock_token: str | None = None
    try:
        # Materializing the previous head both proves unbroken history and runs
        # the same bounded archive/hash checks used by normal execution.
        await cached_repository_model_checkout(candidate, expected_head)
        checkout = await cached_repository_model_checkout(
            candidate,
            candidate.head_commit,
        )
        code_source = CodeSource(
            repo_id=target_repo_id,
            commit=candidate.head_commit,
        )
        node_candidates, model_candidates = _discover_repository_registrations(
            checkout,
            code_source,
        )

        activation_lock, activation_lock_token = await _acquire_registration_lock(
            database,
            target_repo_id,
            task_id,
        )
        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError(
                "workflow repository candidate validation task is no longer active"
            )
        current_target = await database.find_one(
            WorkflowRepo,
            WorkflowRepo.id == target_repo_id,
        )
        current_task = await task_collection.find_one({"_id": raw_task_id})
        if current_target is None or (
            current_target.head_commit != expected_head
            or current_target.archive_sha256 != expected_archive_sha256
        ):
            raise ValueError("workflow repository changed during candidate validation")
        if current_task is None:
            raise RuntimeError("workflow repository candidate validation task was deleted")
        (
            current_target_repo_id,
            current_expected_head,
            current_expected_archive_sha256,
            current_candidate,
        ) = _candidate_payload(current_task)
        if (
            current_target_repo_id != target_repo_id
            or current_expected_head != expected_head
            or current_expected_archive_sha256 != expected_archive_sha256
            or current_candidate.head_commit != candidate.head_commit
            or current_candidate.archive_sha256 != candidate.archive_sha256
            or current_candidate.archive_bytes != candidate.archive_bytes
        ):
            raise ValueError("workflow repository candidate changed during validation")

        conflicts = await _registration_conflicts(
            database,
            target_repo_id,
            node_candidates,
            model_candidates,
        )
        if conflicts:
            raise ValueError("registration conflicts: " + "; ".join(conflicts))

        registered_nodes, registered_models, _, _ = (
            await _validate_registration_tables_without_writes(
                database,
                candidate,
                checkout,
                node_candidates,
                model_candidates,
            )
        )

        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError(
                "workflow repository candidate validation task is no longer active"
            )
        current_target = await database.find_one(
            WorkflowRepo,
            WorkflowRepo.id == target_repo_id,
        )
        current_task = await task_collection.find_one({"_id": raw_task_id})
        if current_target is None or (
            current_target.head_commit != expected_head
            or current_target.archive_sha256 != expected_archive_sha256
        ):
            raise ValueError("workflow repository changed during candidate validation")
        if current_task is None:
            raise RuntimeError("workflow repository candidate validation task was deleted")
        _, _, _, current_candidate = _candidate_payload(current_task)
        if (
            current_candidate.head_commit != candidate.head_commit
            or current_candidate.archive_sha256 != candidate.archive_sha256
            or current_candidate.archive_bytes != candidate.archive_bytes
        ):
            raise ValueError("workflow repository candidate changed during validation")

        logger.info(
            "Workflow repository candidate validated",
            extra={
                "repo_id": str(target_repo_id),
                "candidate_head": candidate.head_commit,
                "nodes": len(registered_nodes),
                "models": len(registered_models),
                "task_id": str(task_id),
            },
        )
        return True
    except BaseException:
        logger.exception(
            "Workflow repository candidate validation failed",
            extra={
                "repo_id": str(target_repo_id),
                "candidate_head": candidate.head_commit,
                "task_id": str(task_id),
            },
        )
        raise
    finally:
        if activation_lock is not None and activation_lock_token is not None:
            await _release_registration_lock(
                activation_lock,
                activation_lock_token,
                target_repo_id,
            )


@node(parameters=Parameters(), expose_in_submit=False)
async def activate_workflow_repo(
    repo: WorkflowRepo,
    **kwargs: Any,
) -> bool:
    """Validate and replace registrations owned by one repository."""

    from simstack.core.context import context

    database = context.db
    task_id = kwargs.get("task_id")
    expected_head = repo.head_commit
    expected_archive_sha256 = repo.archive_sha256
    repository = await database.find_one(WorkflowRepo, WorkflowRepo.id == repo.id)
    if repository is None or (
        repository.head_commit != expected_head
        or repository.archive_sha256 != expected_archive_sha256
    ):
        logger.error(
            "Workflow repository activation target is missing or changed",
            extra={
                "repo_id": str(repo.id),
                "expected_head": expected_head,
                "task_id": str(kwargs.get("task_id")),
            },
        )
        return False

    previous_nodes: list[NodeModel] = []
    previous_models: list[ModelMapping] = []
    registrations_changed = False
    activation_lock: Any | None = None
    activation_lock_token: str | None = None
    if not await _activation_task_is_active(database, task_id):
        logger.warning(
            "Workflow repository activation task is no longer active",
            extra={"repo_id": str(repository.id), "task_id": str(task_id)},
        )
        return False
    if not await _save_repository_state(
        database, repository, WorkflowRepoState.VALIDATING
    ):
        return False
    logger.info(
        "Validating workflow repository",
        extra={
            "repo_id": str(repository.id),
            "head_commit": repository.head_commit,
            "archive_sha256": repository.archive_sha256,
            "task_id": str(kwargs.get("task_id")),
        },
    )

    try:
        code_source = CodeSource(
            repo_id=repository.id,
            commit=repository.head_commit,
        )
        checkout = await cached_repository_checkout(database, code_source)
        node_candidates, model_candidates = _discover_repository_registrations(
            checkout, code_source
        )

        # Registrations are shared by all of a user's runners. Serialize the
        # conflict check and replacement across runner processes so two repos
        # cannot both pass validation and overwrite each other.
        activation_lock, activation_lock_token = await _acquire_registration_lock(
            database,
            repository.id,
            task_id,
        )

        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError("Repository activation task is no longer active")

        current_repository = await database.find_one(
            WorkflowRepo, WorkflowRepo.id == repository.id
        )
        if current_repository is None or (
            current_repository.head_commit != repository.head_commit
            or current_repository.archive_sha256 != repository.archive_sha256
        ):
            raise RuntimeError(
                "Repository changed during validation; retry the current head"
            )
        repository = current_repository
        conflicts = await _registration_conflicts(
            database,
            repository.id,
            node_candidates,
            model_candidates,
        )
        if conflicts:
            raise ValueError("registration conflicts: " + "; ".join(conflicts))

        _, _, previous_nodes, previous_models = (
            await _validate_registration_tables_without_writes(
                database,
                repository,
                checkout,
                node_candidates,
                model_candidates,
            )
        )
        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError("Repository activation task is no longer active")
        current_repository = await database.find_one(
            WorkflowRepo, WorkflowRepo.id == repository.id
        )
        if current_repository is None or (
            current_repository.head_commit != repository.head_commit
            or current_repository.archive_sha256 != repository.archive_sha256
        ):
            raise RuntimeError(
                "Repository changed after table validation; retry the current head"
            )
        repository = current_repository
        # TODO: Once deployment requires replica-set MongoDB, wrap the live
        # registration replacement and READY transition in one transaction.
        # The current standalone-Mongo deployment is exception-recoverable, but a hard
        # worker/process crash can still leave a partial catalog for manual repair.
        registrations_changed = True
        registered_nodes, registered_models = await _build_registration_tables(
            database,
            repository.id,
            checkout,
            node_candidates,
            model_candidates,
            previous_nodes,
            previous_models,
        )

        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError("Repository activation task is no longer active")

        current_repository = await database.find_one(
            WorkflowRepo, WorkflowRepo.id == repository.id
        )
        if current_repository is None or (
            current_repository.head_commit != repository.head_commit
            or current_repository.archive_sha256 != repository.archive_sha256
        ):
            raise RuntimeError(
                "Repository changed while registrations were being updated"
            )

        await context.refresh_mappings()
        if not await _activation_task_is_active(database, task_id):
            raise RuntimeError("Repository activation task is no longer active")
        if not await _save_repository_state(
            database, repository, WorkflowRepoState.READY
        ):
            raise RuntimeError("Repository changed before activation completed")
        logger.info(
            "Workflow repository activated",
            extra={
                "repo_id": str(repository.id),
                "head_commit": repository.head_commit,
                "nodes": len(registered_nodes),
                "models": len(registered_models),
            },
        )
        return True
    except BaseException as exc:
        logger.exception(
            "Workflow repository activation failed",
            extra={
                "repo_id": str(repository.id),
                "head_commit": repository.head_commit,
                "task_id": str(kwargs.get("task_id")),
            },
        )
        try:
            if registrations_changed:
                await _restore_registration_snapshot(
                    database,
                    repository.id,
                    previous_nodes,
                    previous_models,
                )
                await context.refresh_mappings()
        except Exception:
            logger.exception(
                "Workflow repository registration rollback failed",
                extra={"repo_id": str(repository.id)},
            )
        failure_applied = False
        if await _activation_task_is_active(database, task_id):
            failure_applied = await _save_repository_state(
                database,
                repository,
                WorkflowRepoState.FAILED,
                error=str(exc)[:4000],
            )
        if not failure_applied:
            logger.warning(
                "Repository or activation task changed; failure state was not applied",
                extra={
                    "repo_id": str(repository.id),
                    "expected_head": repository.head_commit,
                },
            )
        return False
    finally:
        if activation_lock is not None and activation_lock_token is not None:
            await _release_registration_lock(
                activation_lock,
                activation_lock_token,
                repository.id,
            )
