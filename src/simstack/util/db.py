import logging
from typing import List, Type, TypeVar, Dict, Any, Union, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from odmantic import Model

from simstack.core.definitions import DBType, TaskStatus
from simstack.core.engine import current_engine_context, AIOEngineProxy
from simstack.models import NodeModel
from simstack.models.node_registry import NodeRegistry
from simstack.util.importer import import_class

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Model)


class Database:
    """
    Asynchronous MongoDB database access class using ODMantic ORM.
    Provides a cleaner interface for database operations.
    """

    def __init__(
        self, db_type: DBType, db_name: str = "simstack", connection_string: str = ""
    ):
        """
        Initialize the MongoDB connection

        Args:
            db_type: Type of database configuration
            connection_string: MongoDB connection string (if not using default)
            db_name: Name of the MongoDB database
        """

        if db_type == DBType.IN_MEMORY:
            # For tests, use in-memory MongoDB (mongomock)
            try:
                # import mongomock
                from mongomock_motor import AsyncMongoMockClient

                self.client = AsyncMongoMockClient()
                logger.info("Using in-memory MongoDB mock")
            except ImportError:
                logger.warning(
                    "mongomock not installed, falling back to localhost MongoDB"
                )
                self.client = AsyncIOMotorClient("mongodb://localhost:27017")

        elif db_type == DBType.MONGODB:
            if not connection_string:
                connection_string = "mongodb://localhost:27017"
            self.client = AsyncIOMotorClient(connection_string)
            logger.info("Connected to MongoDB")

        else:
            raise ValueError(f"Unsupported database type for MongoDB: {db_type}")

        # Create engine
        self.engine = AIOEngineProxy(client=self.client, database=db_name)
        # this will set the engine for all functions that are either called from the core package or the server
        current_engine_context.set(self.engine)
        self.db_name = db_name

    async def list_collections(self):
        """
        List all collections in the database
        """
        db = self.client[self.db_name]
        return await db.list_collection_names()

    async def upsert(self, model: Model) -> Model:
        """
        Save or update a model instance including all references and list fields

        Args:
            model: The ODMantic model instance to save

        Returns:
            The saved model with updated fields
        """
        # First, handle all references to ensure they exist in the database
        if isinstance(Model, NodeRegistry):
            if not hasattr(model, "name"):
                logger.exception(
                    f"Fatal Error A trying to save node_registry: {model.model_dump()} for task_id: {model.id}"
                )
                return model
            if model.name is None:
                logger.exception(
                    f"Fatal Error B trying to save node_registry: {model.model_dump()} for task_id: {model.id}"
                )
                return model

        await self._save_references(model)

        # Then save the model itself
        return await self.engine.save(model)

    async def _save_references(self, model: Model, visited=None):
        """
        Recursively save all references within a model

        Args:
            model: The model containing references to save
            visited: Set of object IDs already processed to prevent infinite recursion
        """
        if visited is None:
            visited = set()

        # Skip if we've already processed this object (prevents circular references)
        model_id = id(model)
        if model_id in visited:
            return
        visited.add(model_id)

        # Get all model fields, including those with default_factory
        model_fields = getattr(model.__class__, "model_fields", {})

        # Process each field in the model
        for field_name, field_value in model.__dict__.items():
            if field_value is None:
                continue

            # Get field info if available
            field_info = model_fields.get(field_name)
            if not field_info:
                continue

        # Handle different field types

        # Case 1: Direct Reference fields
        if hasattr(field_info, "annotation") and "Reference" in str(
            field_info.annotation
        ):
            if field_value is not None:
                logger.info(
                    f"Saving reference field {field_name} of type {type(field_value).__name__}"
                )
                await self._save_references(field_value, visited)
                await self.engine.save(field_value)

        # Case 2: List fields that might contain models
        elif isinstance(field_value, list):
            for item in field_value:
                if isinstance(item, Model):
                    logger.info(
                        f"Saving list item of type {type(item).__name__} in field {field_name}"
                    )
                    await self._save_references(item, visited)
                    await self.engine.save(item)

        # Case 3: Embedded models (like in FileInstance within FileStack)
        elif isinstance(field_value, Model):
            logger.info(
                f"Saving embedded model of type {type(field_value).__name__} in field {field_name}"
            )
            await self._save_references(field_value, visited)

    async def save(self, model: Model) -> Model:
        return await self.upsert(model)

    async def find_one(self, model_class: Type[T], query=None, **kwargs) -> Optional[T]:
        """
        Find a single document matching the query
            :param model_class:
            :param query:
        Returns:
            The found model instance or None

        """
        return await self.engine.find_one(model_class, query, **kwargs)

    async def find_one_by_model_name(
        self, model_mapping: str, item_id: str
    ) -> Optional[Any]:
        """
        Find a single document matching the query by model name

        Args:
            model_name: The name of the ODMantic model class as a string

        Returns:
            The found model instance or None

        Raises:
            ValueError: If model name is not found in the global namespace
        """
        # Import common models that might be used

        # Find the model class based on its name
        # model_class = import_class(model_mapping)
        # model_elements = model_mapping.split(".")
        # if len(model_elements) > 1:
        #     model_name = model_mapping.split(".")[-1]
        # else:
        #     model_name = model_mapping
        # # Search through modules in current namespace
        # for module_name, module in sys.modules.items():
        #     if hasattr(module, model_name):
        #         potential_class = getattr(module, model_name)
        #         # Check if it's likely a model class (has attributes like id, __collection__)
        #         if hasattr(potential_class, "id") and hasattr(
        #             potential_class, "__collection__"
        #         ):
        #             model_class = potential_class
        #             break
        # if model_class is None:
        #    logger.info(f"Trying to import model: {model_mapping}")

        model_class = await import_class(model_mapping)
        if model_class is None:
            raise ValueError(
                f"DB: model class {model_mapping} not found in the available modules"
            )

        if isinstance(item_id, str):
            item_id = ObjectId(item_id)

        instance = await self.engine.find_one(model_class, model_class.id == item_id)
        if not instance:
            logger.error(
                f"Instance of '{model_class.__name__}' with id '{item_id}' does not exist"
            )
            raise ValueError(
                f"Instance of '{model_class.__name__}' with id '{item_id}' does not exist"
            )
        return instance

    async def find_all(self, model_class: Type[T], **kwargs) -> List[T]:
        """
        Find all documents of a given model class

        Args:
            model_class: The ODMantic model class
            **kwargs: Query filters
        """
        return await self.engine.find(model_class, **kwargs)

    async def find_many(self, model_class: Type[T], query, **kwargs) -> List[T]:
        """
        Find multiple documents matching the query

        Args:
            model_class: The ODMantic model class
            :param query:
            **kwargs: Query filters

        Returns:
            List of matching model instances

        """
        return await self.engine.find(model_class, query, **kwargs)

    async def delete(self, model: Model) -> None:
        """
        Delete a model instance

        Args:
            model: The model instance to delete
        """
        await self.engine.delete(model)

    async def delete_by_id(
        self, model_class: Type[T], id: Union[str, ObjectId]
    ) -> None:
        """
        Delete a document by its ID

        Args:
            model_class: The ODMantic model class
            id: The document ID (either string or ObjectId)
        """
        # Convert string ID to ObjectId if needed
        if isinstance(id, str):
            id = ObjectId(id)

        instance = await self.engine.find_one(model_class, model_class.id == id)
        if instance:
            await self.engine.delete(instance)
        else:
            logger.error(f"No data found in '{model_class.__name__}' with id '{id}'")

    async def drop_collection(self, model_class: Type[T]) -> None:
        """
        Drop the collection for the given model class

        Args:
            model_class: The ODMantic model class
        """
        collection = self.engine.get_collection(model_class)
        if collection is None:
            logger.error(f"Could not drop collection {model_class.__name__}")
        await collection.drop()

        # collection_name = model_class.__collection__
        # db = self.client[self.db_name]
        # await db[collection_name].drop()

    async def load_from_collection(
        self, model_class: Type[T], id: Union[str, ObjectId]
    ) -> Optional[T]:
        """
        Load a document by its ID

        Args:
            model_class: The ODMantic model class
            id: The document ID (either string or ObjectId)

        Returns:
            The found model instance or None

        Raises:
            ValueError: If document is not found
        """
        # Convert string ID to ObjectId if needed
        if isinstance(id, str):
            id = ObjectId(id)

        instance = await self.engine.find_one(model_class, model_class.id == id)
        if not instance:
            logger.error(f"No data found in '{model_class.__name__}' with id '{id}'")
            raise ValueError(
                f"No data found in '{model_class.__name__}' with id '{id}'"
            )
        return instance

    async def load_task(
        self, name: str, arg_hash: str, function_hash: str
    ) -> Optional[NodeRegistry]:
        """
        Load a task based on name, arg_hash and function_hash

        Args:
            name: Node name
            arg_hash: Hash of the arguments
            function_hash: Hash of the function

        Returns:
            The found NodeRegistry instance or None
        """
        result = await self.engine.find_one(
            NodeRegistry,
            (NodeRegistry.name == name)
            & (NodeRegistry.arg_hash == arg_hash)
            & (NodeRegistry.function_hash == function_hash),
        )
        return result

    async def load_node_model_by_name(
        self, node_model_name: str
    ) -> Optional[NodeModel]:
        """
        Load a node based on its name

        Args:
            node_model_name: The node name

        Returns:
            The found NodeRegistry instance or None
        """
        return await self.engine.find_one(NodeModel, NodeModel.name == node_model_name)

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

        return await self.engine.find_one(NodeRegistry, NodeRegistry.id == task_id)

    async def load_waiting_tasks_for_resource(
        self, resource: str
    ) -> List[NodeRegistry]:
        """
        Load all waiting tasks for a specific resource

        Args:
            resource: The resource name

        Returns:
            List of matching NodeRegistry instances
        """
        submitted_tasks = await self.engine.find(
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
        db = self.client[self.db_name]
        collections = await db.list_collection_names()

        for collection in collections:
            await db[collection].drop()

        logger.info(f"Database {self.db_name} has been reset")

    async def count(self, model_class: Type[T], **kwargs) -> int:
        """
        Count documents matching the query

        Args:
            model_class: The ODMantic model class
            **kwargs: Query filters

        Returns:
            Number of matching documents
        """
        return await self.engine.count(model_class, **kwargs)

    async def aggregate(
        self, model_class: Type[Model], pipeline: List[Dict]
    ) -> List[Dict]:
        """
        Perform an aggregation operation

        Args:
            model_class: The ODMantic model class
            pipeline: MongoDB aggregation pipeline

        Returns:
            List of aggregation results
        """
        collection = self.engine.get_collection(model_class)
        cursor = collection.aggregate(pipeline)
        return await cursor.to_list(length=None)

    async def close(self) -> None:
        """
        Close database connections
        """
        self.client.close()
