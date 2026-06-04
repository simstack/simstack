from __future__ import annotations
import logging
from typing import List,  TypeVar, Union, Optional
from bson import ObjectId
from odmantic import Model

from simstack.core.definitions import DBType, TaskStatus
from simstack.models.node_registry import NodeRegistry
from simstack.util.database_information import DatabaseInformation
# from simstack.util.importer import import_class

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Model)


from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorClient
from odmantic import AIOEngine


class Database:
    """Server-owned database facade.

    The server should depend on this object instead of reaching into SimStack
    core engine abstractions directly.  The underlying persistence primitive is
    the plain ODMantic engine; SimStack-specific behavior lives in this facade.
    """

    def __init__(
        self,
        *,
        client: AsyncIOMotorClient | None = None,
        database_name: str | None = None,
        engine: Any | None = None,
        db_type: DBType | None = None,
    ) -> None:
        if engine is None:
            if client is None or database_name is None:
                raise ValueError("client and database_name are required when engine is not provided")
            engine = AIOEngine(client=client, database=database_name)

        self._db_type = db_type or getattr(engine, "db_type", None)
        self._engine = engine
        self._client = client or getattr(engine, "client", None)
        self._database_name = database_name or getattr(engine, "database_name", None)

        if self._database_name is None:
            database = getattr(engine, "database", None)
            self._database_name = getattr(database, "name", None)

    @property
    def databae_type(self) -> DBType:
        return self._db_type

    @classmethod
    def from_db_info(cls, db_info: DatabaseInformation):
        if db_info.db_type == DBType.IN_MEMORY:
            # For tests, use in-memory MongoDB (mongomock)
            from mongomock_motor import AsyncMongoMockClient
            try:
                # import mongomock
                client = AsyncMongoMockClient()
                logger.info("Using in-memory MongoDB mock")
            except ImportError:
                logger.warning(
                    "mongomock not installed, falling back to localhost MongoDB"
                )
                raise ValueError("mongomock not installed, cannot use in-memory MongoDB")

        elif db_info.db_type == DBType.MONGODB:
            connection_string = db_info.connection_string
            if not connection_string:
                connection_string = "mongodb://localhost:27017"
            client = AsyncIOMotorClient(connection_string)
            logger.info("Connected to MongoDB")
        else:
            raise ValueError(f"Unsupported database type for MongoDB: {db_info.db_type}")

        # Create engine
        engine = AIOEngine(client=client, database=db_info.db_name)
        return cls(engine=engine, client=client, database_name=db_info.db_name)


    @property
    def core_engine(self) -> Any:
        """Compatibility escape hatch for SimStack core internals only."""
        return self._engine

    @property
    def client(self) -> AsyncIOMotorClient:
        return self._client

    @property
    def database_name(self) -> str:
        return self._database_name

    @property
    def raw_database(self):
        if self._client is not None and self._database_name is not None:
            return self._client[self._database_name]
        return getattr(self._engine, "database")

    @property
    def database(self):
        return getattr(self._engine, "database", self.raw_database)

    def collection(self, model_or_name: Any):
        if isinstance(model_or_name, str):
            return self.raw_database[model_or_name]
        return self._engine.get_collection(model_or_name)

    def get_collection(self, model_or_name: Any):
        """Temporary compatibility alias for code still being migrated."""
        return self.collection(model_or_name)

    async def find(self, *args: Any, **kwargs: Any) -> Any:
        return await self._engine.find(*args, **kwargs)

    async def find_one(self, *args: Any, **kwargs: Any) -> Any:
        return await self._engine.find_one(*args, **kwargs)

    async def close(self):
        if self._client is not None:
            self._client.close()

    async def save(self, *args: Any, **kwargs: Any) -> Any:
        if not args:
            return await self._engine.save(*args, **kwargs)

        obj = args[0]
        rest_args = args[1:]
        if isinstance(obj, (list, tuple, set)):
            return [await self._save_one(item, *rest_args, **kwargs) for item in obj]

        return await self._save_one(obj, *rest_args, **kwargs)

    async def save_unchecked(self, *args: Any, **kwargs: Any) -> Any:
        return await self._engine.save(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        return await self._engine.delete(*args, **kwargs)

    # def set_core_context(self):
    #     return current_engine_context.set(self)
    #
    # def reset_core_context(self, token) -> None:
    #     current_engine_context.reset(token)
    #
    # @contextmanager
    # def core_context(self) -> Iterator["Database"]:
    #     token = self.set_core_context()
    #     try:
    #         yield self
    #     finally:
    #         self.reset_core_context(token)

    async def apply_resource_assignment_to_node_registry(self, node_registry: Any) -> Any:
        from simstack.core.resource_assignment import apply_resource_assignment_to_node_registry
        return await apply_resource_assignment_to_node_registry(self, node_registry)

    async def find_artifact_mappings(self, node_registry_path: str,) -> Any:
        from simstack.core.artifacts import find_artifact_mappings
        return await find_artifact_mappings(node_registry_path, self)

    async def find_all_artifacts(self, node_registry: Any) -> Any:
        from simstack.core.artifacts import find_all_artifacts
        return await find_all_artifacts(node_registry, self)

    async def ping(self) -> Any:
        return await self.client.admin.command("ping")

    async def stats(self) -> Any:
        return await self.database.command("dbStats")

    async def _save_one(self, model: Any, *args: Any, **kwargs: Any) -> Any:
        if await self._maybe_call_custom_save(model):
            return None

        parts_saved = await self._call_parts_saves(model)
        if parts_saved:
            return None

        return await self._engine.save(model, *args, **kwargs)

    async def _maybe_call_custom_save(self, target: Any) -> bool:
        save_attr = getattr(target, "save", None)
        if not callable(save_attr):
            return False
        await save_attr(self)
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
        if hasattr(root, "model_dump"):
            try:
                values = root.model_dump().values()
            except Exception:
                return []
        else:
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
        Load a task based on name, arg_hash and function_hash

        Args:
            name: Node name
            arg_hash: Hash of the arguments
            function_hash: Hash of the function

        Returns:
            The found NodeRegistry instance or None
        """
        result = await self.find_one(
            NodeRegistry,
            (NodeRegistry.name == name)
            & (NodeRegistry.arg_hash == arg_hash)
            & (NodeRegistry.function_hash == function_hash),
        )
        return result
    # TODO legacy functions
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
        """
        Load all waiting tasks for a specific resource

        Args:
            resource: The resource name

        Returns:
            List of matching NodeRegistry instances
        """
        submitted_tasks = await self.find(
            NodeRegistry, NodeRegistry.status == TaskStatus.SUBMITTED
        )
        # Then filter them in Python by checking the resource field
        matching_tasks = []
        for task in submitted_tasks:
            # Check if parameters has a resource attribute and if it matches our resource
            # the local runner will also do the immidiate tasks
            if hasattr(task.parameters, "resource") and (
                task.parameters.resource == resource
                or (resource == "local" and task.parameters.resource == "self")
            ):
                if resource == "local" and task.parameters.resource == "self":
                    logger.info(f"local runner taking job for 'self' with  {task.id}")
                matching_tasks.append(task)
        return matching_tasks

    async def reset_database(self) -> None:
        """
        Reset the database by dropping all collections and recreating them
        """
        db = self.client[self.database_name]
        collections = await db.list_collection_names()

        for collection in collections:
            await db[collection].drop()

        logger.info(f"Database {self.database_name} has been reset")


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


#
# def set_database_core_context(database: Any):
#     set_context = getattr(database, "set_core_context", None)
#     if callable(set_context):
#         return set_context()
#     return current_engine_context.set(database)
#
#
# def reset_database_core_context(database: Any, token) -> None:
#     reset_context = getattr(database, "reset_core_context", None)
#     if callable(reset_context):
#         reset_context(token)
#         return
#     current_engine_context.reset(token)
#

# TODO engines
async def find_all_artifacts_for_database(database: Database, node_registry: Any) -> Any:
    find_all = getattr(database, "find_all_artifacts", None)
    if callable(find_all):
        return await database.find(node_registry)

    from simstack.core.artifacts import find_all_artifacts
    return await find_all_artifacts(node_registry, database)

#
# class DatabaseOld(DatabaseInformation):
#     """
#     Asynchronous MongoDB database access class using ODMantic ORM.
#     Provides a cleaner interface for database operations.
#     """
#
#     def __init__(self, db_type: DBType, db_name: str = "simstack", connection_string: str = ""):
#         super().__init__(db_name, connection_string, db_type)
#         """
#         Initialize the MongoDB connection
#
#         Args:
#             db_type: Type of database configuration
#             connection_string: MongoDB connection string (if not using default)
#             db_name: Name of the MongoDB database
#         """
#
#         if db_type == DBType.IN_MEMORY:
#             # For tests, use in-memory MongoDB (mongomock)
#             try:
#                 # import mongomock
#                 from mongomock_motor import AsyncMongoMockClient
#
#                 self.client = AsyncMongoMockClient()
#                 logger.info("Using in-memory MongoDB mock")
#             except ImportError:
#                 logger.warning(
#                     "mongomock not installed, falling back to localhost MongoDB"
#                 )
#                 self.client = AsyncIOMotorClient("mongodb://localhost:27017")
#
#         elif db_type == DBType.MONGODB:
#             if not connection_string:
#                 connection_string = "mongodb://localhost:27017"
#             self.client = AsyncIOMotorClient(connection_string)
#             logger.info("Connected to MongoDB")
#
#         else:
#             raise ValueError(f"Unsupported database type for MongoDB: {db_type}")
#
#         # Create engine
#         self.engine = AIOEngineProxy(client=self.client, database=db_name)
#         # this will set the engine for all functions that are either called from the core package or the server
#         # current_engine_context.set(self.engine)
#
#
#     @classmethod
#     def from_db_info(cls, db_info: DatabaseInformation):
#         return cls(db_info.db_type, db_info.db_name, db_info.connection_string)
#
#     async def list_collections(self):
#         """
#         List all collections in the database
#         """
#         db = self.client[self.db_name]
#         return await db.list_collection_names()
#
#     async def upsert(self, model: Model) -> Model:
#         """
#         Save or update a model instance including all references and list fields
#
#         Args:
#             model: The ODMantic model instance to save
#
#         Returns:
#             The saved model with updated fields
#         """
#         # First, handle all references to ensure they exist in the database
#         if isinstance(Model, NodeRegistry):
#             if not hasattr(model, "name"):
#                 logger.exception(
#                     f"Fatal Error A trying to save node_registry: {model.model_dump()} for task_id: {model.id}"
#                 )
#                 return model
#             if model.name is None:
#                 logger.exception(
#                     f"Fatal Error B trying to save node_registry: {model.model_dump()} for task_id: {model.id}"
#                 )
#                 return model
#
#         await self._save_references(model)
#
#         # Then save the model itself
#         return await self.engine.save(model)
#
#     async def _save_references(self, model: Model, visited=None):
#         """
#         Recursively save all references within a model
#
#         Args:
#             model: The model containing references to save
#             visited: Set of object IDs already processed to prevent infinite recursion
#         """
#         if visited is None:
#             visited = set()
#
#         # Skip if we've already processed this object (prevents circular references)
#         model_id = id(model)
#         if model_id in visited:
#             return
#         visited.add(model_id)
#
#         # Get all model fields, including those with default_factory
#         model_fields = getattr(model.__class__, "model_fields", {})
#
#         # Process each field in the model
#         for field_name, field_value in model.__dict__.items():
#             if field_value is None:
#                 continue
#
#             # Get field info if available
#             field_info = model_fields.get(field_name)
#             if not field_info:
#                 continue
#
#         # Handle different field types
#
#         # Case 1: Direct Reference fields
#         if hasattr(field_info, "annotation") and "Reference" in str(
#             field_info.annotation
#         ):
#             if field_value is not None:
#                 logger.info(
#                     f"Saving reference field {field_name} of type {type(field_value).__name__}"
#                 )
#                 await self._save_references(field_value, visited)
#                 await self.engine.save(field_value)
#
#         # Case 2: List fields that might contain models
#         elif isinstance(field_value, list):
#             for item in field_value:
#                 if isinstance(item, Model):
#                     logger.info(
#                         f"Saving list item of type {type(item).__name__} in field {field_name}"
#                     )
#                     await self._save_references(item, visited)
#                     await self.engine.save(item)
#
#         # Case 3: Embedded models (like in FileInstance within FileStack)
#         elif isinstance(field_value, Model):
#             logger.info(
#                 f"Saving embedded model of type {type(field_value).__name__} in field {field_name}"
#             )
#             await self._save_references(field_value, visited)
#
#     async def save(self, model: Model) -> Model:
#         return await self.upsert(model)
#
#     async def find_one(self, model_class: Type[T], query=None, **kwargs) -> Optional[T]:
#         """
#         Find a single document matching the query
#             :param model_class:
#             :param query:
#         Returns:
#             The found model instance or None
#
#         """
#         return await self.engine.find_one(model_class, query, **kwargs)
#
#     async def find_one_by_model_name(
#         self, model_mapping: str, item_id: str
#     ) -> Optional[Any]:
#         """
#         Find a single document matching the query by model name
#
#         Args:
#             model_name: The name of the ODMantic model class as a string
#
#         Returns:
#             The found model instance or None
#
#         Raises:
#             ValueError: If model name is not found in the global namespace
#         """
#         # Import common models that might be used
#
#         # Find the model class based on its name
#         # model_class = import_class(model_mapping)
#         # model_elements = model_mapping.split(".")
#         # if len(model_elements) > 1:
#         #     model_name = model_mapping.split(".")[-1]
#         # else:
#         #     model_name = model_mapping
#         # # Search through modules in current namespace
#         # for module_name, module in sys.modules.items():
#         #     if hasattr(module, model_name):
#         #         potential_class = getattr(module, model_name)
#         #         # Check if it's likely a model class (has attributes like id, __collection__)
#         #         if hasattr(potential_class, "id") and hasattr(
#         #             potential_class, "__collection__"
#         #         ):
#         #             model_class = potential_class
#         #             break
#         # if model_class is None:
#         #    logger.info(f"Trying to import model: {model_mapping}")
#
#         from simstack.util.importer import import_class
#         model_class = await import_class(model_mapping, self)
#         if model_class is None:
#             raise ValueError(
#                 f"DB: model class {model_mapping} not found in the available modules"
#             )
#
#         if isinstance(item_id, str):
#             item_id = ObjectId(item_id)
#
#         instance = await self.engine.find_one(model_class, model_class.id == item_id)
#         if not instance:
#             logger.error(
#                 f"Instance of '{model_class.__name__}' with id '{item_id}' does not exist"
#             )
#             raise ValueError(
#                 f"Instance of '{model_class.__name__}' with id '{item_id}' does not exist"
#             )
#         return instance
#
#     async def find_all(self, model_class: Type[T], **kwargs) -> List[T]:
#         """
#         Find all documents of a given model class
#
#         Args:
#             model_class: The ODMantic model class
#             **kwargs: Query filters
#         """
#         return await self.engine.find(model_class, **kwargs)
#
#     async def find_many(self, model_class: Type[T], query, **kwargs) -> List[T]:
#         """
#         Find multiple documents matching the query
#
#         Args:
#             model_class: The ODMantic model class
#             :param query:
#             **kwargs: Query filters
#
#         Returns:
#             List of matching model instances
#
#         """
#         return await self.engine.find(model_class, query, **kwargs)
#
#     async def delete(self, model: Model) -> None:
#         """
#         Delete a model instance
#
#         Args:
#             model: The model instance to delete
#         """
#         await self.engine.delete(model)
#
#     async def delete_by_id(
#         self, model_class: Type[T], id: Union[str, ObjectId]
#     ) -> None:
#         """
#         Delete a document by its ID
#
#         Args:
#             model_class: The ODMantic model class
#             id: The document ID (either string or ObjectId)
#         """
#         # Convert string ID to ObjectId if needed
#         if isinstance(id, str):
#             id = ObjectId(id)
#
#         instance = await self.engine.find_one(model_class, model_class.id == id)
#         if instance:
#             await self.engine.delete(instance)
#         else:
#             logger.error(f"No data found in '{model_class.__name__}' with id '{id}'")
#
#     async def drop_collection(self, model_class: Type[T]) -> None:
#         """
#         Drop the collection for the given model class
#
#         Args:
#             model_class: The ODMantic model class
#         """
#         collection = self.engine.get_collection(model_class)
#         if collection is None:
#             logger.error(f"Could not drop collection {model_class.__name__}")
#         await collection.drop()
#
#         # collection_name = model_class.__collection__
#         # db = self.client[self.db_name]
#         # await db[collection_name].drop()
#
#     async def load_from_collection(
#         self, model_class: Type[T], id: Union[str, ObjectId]
#     ) -> Optional[T]:
#         """
#         Load a document by its ID
#
#         Args:
#             model_class: The ODMantic model class
#             id: The document ID (either string or ObjectId)
#
#         Returns:
#             The found model instance or None
#
#         Raises:
#             ValueError: If document is not found
#         """
#         # Convert string ID to ObjectId if needed
#         if isinstance(id, str):
#             id = ObjectId(id)
#
#         instance = await self.engine.find_one(model_class, model_class.id == id)
#         if not instance:
#             logger.error(f"No data found in '{model_class.__name__}' with id '{id}'")
#             raise ValueError(
#                 f"No data found in '{model_class.__name__}' with id '{id}'"
#             )
#         return instance
#
#     async def load_node_model_by_name(
#         self, node_model_name: str
#     ) -> Optional["NodeModel"]:
#         """
#         Load a node based on its name
#
#         Args:
#             node_model_name: The node name
#
#         Returns:
#             The found NodeRegistry instance or None
#         """
#         from simstack.models import NodeModel
#         return await self.engine.find_one(NodeModel, NodeModel.name == node_model_name)
#
#     async def load_task_by_id(
#         self, task_id: Union[str, ObjectId]
#     ) -> Optional[NodeRegistry]:
#         """
#         Load a task based on its ID
#
#         Args:
#             task_id: The task ID
#
#         Returns:
#             The found NodeRegistry instance or None
#         """
#         if isinstance(task_id, str):
#             task_id = ObjectId(task_id)
#
#         return await self.engine.find_one(NodeRegistry, NodeRegistry.id == task_id)
#
#     async def load_waiting_tasks_for_resource(
#         self, resource: str
#     ) -> List[NodeRegistry]:
#         """
#         Load all waiting tasks for a specific resource
#
#         Args:
#             resource: The resource name
#
#         Returns:
#             List of matching NodeRegistry instances
#         """
#         submitted_tasks = await self.engine.find(
#             NodeRegistry, NodeRegistry.status == TaskStatus.SUBMITTED
#         )
#         # Then filter them in Python by checking the resource field
#         matching_tasks = []
#         for task in submitted_tasks:
#             # Check if parameters has a resource attribute and if it matches our resource
#             # the local runner will also do the immidiate tasks
#             if hasattr(task.parameters, "resource") and (
#                 task.parameters.resource == resource
#                 or (resource == "local" and task.parameters.resource == "self")
#             ):
#                 if resource == "local" and task.parameters.resource == "self":
#                     logger.info(f"local runner taking job for 'self' with  {task.id}")
#                 matching_tasks.append(task)
#         return matching_tasks
#
#     async def reset_database(self) -> None:
#         """
#         Reset the database by dropping all collections and recreating them
#         """
#         db = self.client[self.db_name]
#         collections = await db.list_collection_names()
#
#         for collection in collections:
#             await db[collection].drop()
#
#         logger.info(f"Database {self.db_name} has been reset")
#
#     async def count(self, model_class: Type[T], **kwargs) -> int:
#         """
#         Count documents matching the query
#
#         Args:
#             model_class: The ODMantic model class
#             **kwargs: Query filters
#
#         Returns:
#             Number of matching documents
#         """
#         return await self.engine.count(model_class, **kwargs)
#
#     async def aggregate(
#         self, model_class: Type[Model], pipeline: List[Dict]
#     ) -> List[Dict]:
#         """
#         Perform an aggregation operation
#
#         Args:
#             model_class: The ODMantic model class
#             pipeline: MongoDB aggregation pipeline
#
#         Returns:
#             List of aggregation results
#         """
#         collection = self.engine.get_collection(model_class)
#         cursor = collection.aggregate(pipeline)
#         return await cursor.to_list(length=None)
#
#     async def close(self) -> None:
#         """
#         Close database connections
#         """
#         self.client.close()
