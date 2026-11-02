from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional, TypeVar, Union

from simstack.core.definitions import DBType, TaskStatus
from simstack.models.document import EmbeddedModel, Model, ObjectId
from simstack.models.node_registry import NodeRegistry

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=Model)


class Database:
    """Compatibility facade for workflow hooks over a neutral document repository.

    Runner repositories perform HTTP requests only. Database drivers belong to
    the Server, which injects its own backend explicitly.
    """

    def __init__(
        self,
        *,
        engine=None,
        repository=None,
        db_type=None,
        server_url=None,
        server_token=None,
        database_name=None,
        **kwargs,
    ):
        if kwargs:
            raise ValueError("Database clients and connection strings are Server-owned")
        self._repository = repository or engine
        if self._repository is None:
            raise ValueError("A document repository is required")
        self._engine = self._repository  # compatibility for Server integrations
        self._db_type = db_type or getattr(self._repository, "db_type", None)
        self._database_name = database_name or getattr(
            self._repository, "database_name", None
        )
        self._server_url, self._server_token = server_url, server_token

    @classmethod
    def from_db_info(cls, db_info):
        if db_info.db_type == DBType.API:
            from simstack.runner_api.repository import HTTPRepository
            from simstack.runner_api.transport import RunnerAPIClient

            return cls(
                repository=HTTPRepository(
                    RunnerAPIClient(db_info.server_url, db_info.server_token)
                ),
                db_type=DBType.API,
                server_url=db_info.server_url,
                server_token=db_info.server_token,
            )
        if db_info.db_type == DBType.IN_MEMORY:
            from simstack.persistence.memory import InMemoryRepository

            return cls(
                repository=InMemoryRepository(),
                db_type=DBType.IN_MEMORY,
                database_name=db_info.db_name,
                server_url=db_info.server_url,
                server_token=db_info.server_token,
            )
        raise ValueError(
            "Direct database connections are Server-owned; Runner requires API credentials"
        )

    @property
    def databae_type(self):
        return self._db_type

    @property
    def connection_string(self):
        return None

    @property
    def database_name(self):
        return self._database_name

    @property
    def server_url(self):
        return self._server_url

    @property
    def server_token(self):
        return self._server_token

    @property
    def core_engine(self):
        return self._repository

    @property
    def raw_database(self):
        if self._db_type == DBType.API:
            raise RuntimeError("Runner cannot access database collections")
        return self._repository.database

    @property
    def database(self):
        return self.raw_database

    @property
    def client(self):
        if self._db_type == DBType.API:
            raise RuntimeError("Runner has no database client")
        return self._repository.client

    def collection(self, model_or_name):
        if self._db_type == DBType.API:
            raise RuntimeError("Runner cannot access database collections")
        return self._repository.get_collection(model_or_name)

    get_collection = collection

    async def find(self, model, *criteria, **kwargs):
        results = await self._repository.find(model, *criteria, **kwargs)
        for result in results:
            try:
                await self._apply_postprocess(model, result)
            except Exception:
                # Keep the existing facade's load-hook error behavior: the
                # hook reports its error and the persisted model remains usable.
                pass
        return results

    find_all = find

    async def find_one(self, model, *criteria, **kwargs):
        result = await self._repository.find_one(model, *criteria, **kwargs)
        if result is not None:
            try:
                await self._apply_postprocess(model, result)
            except Exception:
                pass
        return result

    async def find_one_by_model_name(self, model_mapping, item_id):
        from simstack.util.importer import import_class, import_class_by_name

        model = await (
            import_class(model_mapping, self)
            if "." in model_mapping
            else import_class_by_name(model_mapping, self)
        )
        if model is None:
            raise ValueError(f"Model {model_mapping} is not registered")
        from pydantic import TypeAdapter

        identity_field = model.model_fields[model.__primary_field__]
        identity = TypeAdapter(identity_field.annotation).validate_python(item_id)
        result = await self.find_one(
            model, getattr(model, model.__primary_field__) == identity
        )
        if result is None:
            raise ValueError(f"Model {model_mapping}:{item_id} does not exist")
        return result

    async def _apply_postprocess(self, model_class: type, result: Model) -> None:
        """Apply db_find_postprocess to results if defined on the model class."""
        if isinstance(result, NodeRegistry):
            from simstack.core.services.node_registry_service import (
                remember_user_editable_fields,
            )

            remember_user_editable_fields(result)

        model_class = type(result)
        post_process = getattr(model_class, "db_find_postprocess", None)

        if post_process and callable(post_process):
            import inspect

            is_async = inspect.iscoroutinefunction(post_process)

            try:
                if is_async:
                    await post_process(result, self)
                else:
                    logger.warning(
                        f"Sync db_find_postprocess on {model_class.__name__} is deprecated. "
                        "Please make it asynchronous."
                    )
                    post_process(result, self)
            except Exception as e:
                logger.error(
                    f"Error during post-processing {model_class.__name__}: {e}",
                    exc_info=True,
                )
                # Should we re-raise or just log? Usually post-processing failure is critical.
                raise

        # Recursively apply postprocess to nested Model attributes
        if not isinstance(result, Model) or isinstance(result, EmbeddedModel):
            return

        try:
            for attr_name in dir(result):
                if attr_name.startswith("_") or attr_name in {
                    "model_fields",
                    "model_computed_fields",
                }:
                    continue

                try:
                    attr_value = getattr(result, attr_name, None)
                except Exception:
                    continue

                if attr_value is None or callable(attr_value):
                    continue

                # Handle direct Model instance
                if isinstance(attr_value, Model) or isinstance(
                    attr_value, EmbeddedModel
                ):
                    await self._apply_postprocess(type(attr_value), attr_value)

                # Handle list of Models
                elif isinstance(attr_value, (list, tuple)):
                    for item in attr_value:
                        if isinstance(item, Model) or isinstance(item, EmbeddedModel):
                            await self._apply_postprocess(type(item), item)

                # Handle dict with Model values
                elif isinstance(attr_value, dict):
                    for value in attr_value.values():
                        if isinstance(value, Model) or isinstance(value, EmbeddedModel):
                            await self._apply_postprocess(type(value), value)

        except Exception as e:
            logger.error(
                f"Error during recursive post-processing traversal for {model_class.__name__}: {e}",
                exc_info=True,
            )

    async def close(self):
        close = getattr(self._repository, "close", None)
        if close:
            import inspect

            result = close()
            if inspect.isawaitable(result):
                await result

    async def save(self, obj, **kwargs):
        if isinstance(obj, (list, tuple, set)):
            return [await self._save_one(item, **kwargs) for item in obj]
        return await self._save_one(obj, **kwargs)

    async def save_all(self, objects, **kwargs):
        return await self.save(list(objects), **kwargs)

    async def save_unchecked(self, obj, **kwargs):
        self._prepare_file_storage(obj)
        return await self._repository.save(obj, **kwargs)

    async def delete(self, obj, **kwargs):
        return await self._repository.delete(obj, **kwargs)

    async def task_mutation(self, task_id, action, values=None, expected_statuses=None):
        return await self._repository.task_mutation(
            str(task_id), action, values, expected_statuses
        )

    async def apply_resource_assignment_to_node_registry(self, entry):
        from simstack.core.resource_assignment import (
            apply_resource_assignment_to_node_registry,
        )

        return await apply_resource_assignment_to_node_registry(self, entry)

    async def find_artifact_mappings(self, path):
        from simstack.core.artifacts import find_artifact_mappings

        return await find_artifact_mappings(path, self)

    async def find_all_artifacts(self, entry):
        from simstack.core.artifacts import find_all_artifacts

        return await find_all_artifacts(entry, self)

    async def ping(self):
        ping = getattr(self._repository, "ping", None)
        return await ping() if ping else True

    async def stats(self):
        stats = getattr(self._repository, "stats", None)
        return await stats() if stats else {}

    @staticmethod
    def _prepare_file_storage(root):
        from simstack.models.files import FileStack

        seen = set()

        def prepare(value):
            if id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, FileStack):
                value.ensure_uploaded()
            if isinstance(value, (Model, EmbeddedModel)):
                for child in vars(value).values():
                    prepare(child)
            elif isinstance(value, dict):
                for child in value.values():
                    prepare(child)
            elif isinstance(value, (list, tuple, set)):
                for child in value:
                    prepare(child)

        prepare(root)

    async def _save_one(self, model: Any, *args: Any, **kwargs: Any) -> Any:
        self._prepare_file_storage(model)
        # Avoid recursion if custom save is called
        if getattr(model, "_currently_saving", False):
            result = await self._repository.save(model, *args, **kwargs)
            return result if result is not None else model

        custom_save_called = await self._maybe_call_custom_save(model)

        # Parts saves should always be called unless the main model has a custom save
        # that handles everything. But in our current logic, _maybe_call_custom_save
        # returning True means the custom save WAS called.
        if not custom_save_called:
            await self._call_parts_saves(model)

        if custom_save_called:
            return model

        if isinstance(model, NodeRegistry):
            from simstack.core.services.node_registry_service import (
                apply_persisted_user_editable_fields,
                remember_user_editable_fields,
            )

            await apply_persisted_user_editable_fields(self, model)
            result = await self._repository.save(model, *args, **kwargs)
            remember_user_editable_fields(model)
            return result if result is not None else model

        result = await self._repository.save(model, *args, **kwargs)
        return result if result is not None else model

    async def _maybe_call_custom_save(self, target: Any) -> bool:
        save_attr = getattr(target, "save", None)
        if not callable(save_attr):
            return False

        # Avoid recursion if custom save is called
        if getattr(target, "_currently_saving", False):
            return False

        # Set a flag to prevent recursion if custom save calls db.save(self)
        object.__setattr__(target, "_currently_saving", True)
        try:
            await save_attr(self)
        finally:
            object.__setattr__(target, "_currently_saving", False)
        return True

    async def _call_parts_saves(self, model: Any) -> bool:
        any_saved = False
        seen_ids: set[int] = set()

        for part in self._iter_save_parts(model):
            part_id = id(part)
            if part_id in seen_ids:
                continue
            seen_ids.add(part_id)

            if await self._maybe_call_custom_save(part):
                any_saved = True

        return any_saved

    @staticmethod
    def _iter_save_parts(root: Any) -> Iterable[Any]:
        try:
            values = list(vars(root).values())
        except TypeError:
            return []

        parts: list[Any] = []
        for value in values:
            parts.append(value)
            if isinstance(value, (list, tuple, set)):
                parts.extend(value)
            elif isinstance(value, dict):
                parts.extend(value.values())
        return parts

    async def load_task(
        self, name: str, arg_hash: str, function_hash: str
    ) -> Optional["NodeRegistry"]:
        """
        Load a task based on name and arg_hash.

        Args:
            name: Node name
            arg_hash: Hash of the arguments
            function_hash: Unused; kept for call-site compatibility.

        Returns:
            The found NodeRegistry instance or None
        """
        result = await self.find_one(
            NodeRegistry,
            (NodeRegistry.name == name) & (NodeRegistry.arg_hash == arg_hash),
        )
        return result

    # legacy functions ... these are functions in the old database class which we do not want to migrate if possible
    #
    # load_waiting_tasks_for_resource DONE
    # reset_database                  DONE
    # the rest is hopefully not needed anymore
    # list_collections
    # upsert
    # _save_references
    # find_one_by_model_name
    # find_all
    # find_many
    # delete_by_id
    # drop_collection
    # load_from_collection
    # load_node_model_by_name
    # load_task_by_id
    # count
    # aggregate

    async def load_waiting_tasks_for_resource(
        self, resource: str
    ) -> List["NodeRegistry"]:
        """Load submitted tasks for a specific resource."""
        return await self.load_tasks_for_resource(resource, TaskStatus.SUBMITTED)

    async def load_terminating_tasks_for_resource(
        self, resource: str
    ) -> List["NodeRegistry"]:
        """Load tasks the runner should kill on this resource."""
        return await self.load_tasks_for_resource(resource, TaskStatus.TERMINATING)

    async def load_tasks_for_resource(
        self, resource: str, status: TaskStatus
    ) -> List["NodeRegistry"]:
        """
        Load tasks in ``status`` for a specific resource.

        Args:
            resource: The resource name
            status: Registry status to load

        Returns:
            List of matching NodeRegistry instances
        """
        if not isinstance(status, TaskStatus):
            raise ValueError(f"status must be a TaskStatus, got {status!r}")
        submitted_tasks = await self.find(NodeRegistry, NodeRegistry.status == status)

        # Then filter them in Python by checking the resource field
        matching_tasks = []
        for task in submitted_tasks:
            # Check if parameters has a resource attribute and if it matches our resource
            # the local runner will also do the immidiate tasks
            # TODO not the local runner the default resource should pick up self but there should be no seld
            if (
                hasattr(task.parameters, "resource")
                and task.parameters.resource == resource
            ):
                matching_tasks.append(task)
            if (
                hasattr(task.parameters, "resource")
                and task.parameters.resource == "self"
            ):
                logger.error("There should be tasks submitted to self")
        return matching_tasks

    async def reset_database(self) -> None:
        """
        Reset the database by dropping all collections and recreating them
        """
        if self._db_type == DBType.API:
            raise RuntimeError("Database reset is a server administration operation")
        reset = getattr(self._repository, "reset", None)
        if reset is None:
            raise RuntimeError("Database reset is a server administration operation")
        await reset()

    async def load_task_by_id(
        self, task_id: Union[str, ObjectId]
    ) -> Optional[NodeRegistry]:
        """
        Load a task based on its ID

        Args:
            task_id: The task ID

        Returns:
            The found NodeRegistry instance or None
        """
        if isinstance(task_id, str):
            task_id = ObjectId(task_id)

        return await self.find_one(NodeRegistry, NodeRegistry.id == task_id)
