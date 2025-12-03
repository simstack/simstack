import asyncio
import functools
import inspect
import logging
import os
from datetime import datetime
try:
    from multiprocessing.reduction import duplicate
except ImportError:
    import os


    def duplicate(handle, target_process=None):
        return os.dup(handle)

from pathlib import Path
from typing import Callable, Optional, TypeVar, cast, List, ParamSpec, Union, overload

import coolname
import nest_asyncio
from odmantic import Model, ObjectId

from simstack.core.artifacts import create_artifacts, ArtifactArguments
from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.engine import current_engine_context
from simstack.core.hash import complex_hash_function
from simstack.core.node_runner import NodeRunner
from simstack.core.simstack_result import SimstackResult
from simstack.core.task_id import set_task_id, clear_task_id
from simstack.models import ModelMapping, Parameters
from simstack.models import NodeModel
from simstack.models import NodeRegistry
from simstack.models.file_list import FileListModel
from simstack.models.files import FileStack
from simstack.models.parameters import Resource
from simstack.models.simstack_model import is_simstack_model
from simstack.util.importer import import_function, import_class

logger = logging.getLogger("Node")

nest_asyncio.apply()

T = TypeVar("T")


def default_name_generator():
    return "-".join(coolname.generate(2))


def hashable_inputs(arg):
    """
    Get the hashable inputs for the node. This allows exclusion of some fields from the hash.

    Returns:
        dict: The hashable inputs.
    """
    return {key: value for key, value in arg.__dict__.items() if key not in ["id"]}


def compute_arg_hash(args: List[Model]) -> str:
    """
    Computes a hash for a list of arguments provided, where each argument
    is an instance of the Model class or can be processed into a hashable
    format. Uses a complex hashing function for the resulting computation.

    Parameters:
    args: List[Model]
        A list of objects where each object must be an instance of the
        Model class. The objects are used to compute their respective
        hash values via a specified complex hashing mechanism.

    Returns:
    str
        A string representation of the computed hash for the provided
        list of arguments.

    Raises:
    TypeError
        If any item in the provided list is not an instance of the
        Model class.
    """
    arg_hashes = []
    for arg in args:
        if not isinstance(arg, Model):
            raise TypeError(f"Argument {arg} is not an instance of {Model}")
        arg_hash = (
            arg.complex_hash()
            if hasattr(arg, "complex_hash")
            else complex_hash_function(hashable_inputs(arg))
        )
        arg_hashes.append(arg_hash)
    return complex_hash_function(arg_hashes)

class Node:
    """
    represents a computational task or node in the workflow, managing its lifecycle,
    execution environment, and interactions with the database. This class helps in
    defining tasks, storing their details, and ensuring they are executed either
    locally or remotely, with their statuses tracked within a shared database
    context.

    :ivar name: The name of the function associated with the node.
    :type name: str
    :ivar custom_name: A custom name for the node, generated if not specified.
    :type custom_name: str or None
    :ivar registry_entry: The registry entry associated with the node in the database.
    :type registry_entry: NodeRegistry or None
    :ivar parent_id: The parent node's unique identifier, if applicable.
    :type parent_id: ObjectId or None
    :ivar parameters: Additional parameters for the node.
    :type parameters: Parameters
    :ivar _func: The function represented by the node.
    :type _func: Callable[[Model], Model]
    :ivar *args: The arguments passed to the function.
    :type *args: List[Model]
    """

    def __init__(
        self,
        *args: List[Model],
        **kwargs,
    ):
        super().__init__()

        self._args = list(args)  # Convert to list to allow appending

        # Extract specific known parameters
        self._func = kwargs.pop("func")
        self.name = self._func.__name__
        self.is_async = kwargs.pop("is_async")
        self.parent_id = kwargs.pop("parent_id", None)
        self.call_path = kwargs.pop("call_path", "")

        # Get function signature to identify argument names
        sig = inspect.signature(self._func)
        param_names = list(sig.parameters.keys())

        # Move kwargs that match function parameter names to args
        for param_name in param_names:
            if param_name in kwargs:
                self._args.append(kwargs.pop(param_name))

        if "custom_name" in kwargs:
            self.custom_name = kwargs["custom_name"]  # pass to children
        else:
            self.custom_name = default_name_generator()

        self.parameters = kwargs.pop("parameters")
        self.recompute_artifacts = kwargs.pop("recompute_artifacts", False) or (
            self.parameters.recompute_artifacts or False
        )

        self._function_kwargs = (
            kwargs  # what is left over here must be kwargs of the function
        )
        self.registry_entry = None

    @property
    def id(self):
        if self.registry_entry is None:
            return None
        else:
            return self.registry_entry.id

    @property
    def status(self):
        return getattr(self.registry_entry, "status", TaskStatus.FAILED)

    async def make_registry_entry(self, function_hash, arg_hash) -> NodeRegistry:
        """
        Creates a registry entry for the node in the database.

        This method is used to create a new entry in the database for the node,
        including its inputs and outputs. It ensures that the task is properly
        registered with all necessary details.

        :rtype: NodeRegistry
        """

        function_mapping = await context.db.find_one(NodeModel, NodeModel.name == self.name)
        if function_mapping is None:
            logger.error(f"Could not find function mapping for name: {self.name}")
            raise ValueError(f"Could not find function mapping for name: {self.name}")

        input_ids = []
        input_tables = []
        for arg in self._args:
            # if there is no table for an arg raise an error
            input_table_name = await context.db.find_one(
                ModelMapping, ModelMapping.name == arg.__class__.__name__
            )
            if input_table_name is None:
                logger.error(f"Could not find table name for {arg.__class__.__name__}")
                raise ValueError(
                    f"Could not find table name for {arg.__class__.__name__}"
                )
            if not isinstance(arg, Model):
                logger.error(f"{arg.__class__.__name__} is not an odmantic Model")
                raise ValueError(f"{arg.__class__.__name__} is not an odmantic Model")

            argument_entry = await context.db.upsert(arg)

            # Check if the upsert operation was successful and returned a valid ID
            if argument_entry is None or argument_entry.id is None:
                logger.error(
                    f"Failed to upsert argument {arg} - returned None or invalid ID"
                )
                raise ValueError(
                    f"Failed to upsert argument of type {arg.__class__.__name__}"
                )

            input_ids.append(argument_entry.id)
            input_tables.append(input_table_name.mapping)

        new_parameters = Parameters(**self.parameters.__dict__)

        self.registry_entry = NodeRegistry(
            name=self.name,
            input_tables=input_tables,
            input_ids=input_ids,
            is_async=self.is_async,
            status=TaskStatus.SUBMITTED,
            custom_name=self.custom_name,
            function_hash=function_hash,
            arg_hash=arg_hash,
            parent_ids=[] if self.parent_id is None else [self.parent_id],
            parameters=new_parameters,
            func_mapping=function_mapping.function_mapping,
            call_path=self.call_path,
        )
        await context.db.upsert(self.registry_entry)
        logger.info(
            f"Task task_id: {self.id} with name {self.name} created for resource: {new_parameters.resource} queue: {new_parameters.queue}"
        )
        return self.registry_entry



    async def get_node_registry(self) -> TaskStatus:
        """
        Reads or initializes the task registry entry in the database.

        This method ensures that a task entry exists in the database for the
        current task. It computes hashes of its arguments and function,
        checks if a database entry already matches these hashes, and creates
        a new entry if no match is found. If the database is not connected,
        an exception is raised.

        :raises ValueError: if the database is not connected.
        :return: Status of the task retrieved or created.
        :rtype: TaskStatus
        """
        if context.db is None:
            raise ValueError("Database is not connected")

        arg_hash = compute_arg_hash(self._args)
        function_hash = complex_hash_function(self._func)

        self.registry_entry = (
            await context.db.load_task(self.name, arg_hash, function_hash)
            if not self.parameters.force_rerun
            else None
        )

        if self.registry_entry is None:
            await self.make_registry_entry(function_hash, arg_hash)
        else:
            if self.parent_id:
                logger.info(
                    f"Task task_id: {self.id} adding parent_id {self.parent_id} to task: {self.name}"
                )
                if isinstance(self.parent_id, str):
                    logger.error(
                        f"Task task_id: {self.id} parent_id is a string: {self.parent_id}"
                    )
                    self.parent_id = ObjectId(self.parent_id)
                self.registry_entry.parent_ids.append(self.parent_id)
                await context.db.save(self.registry_entry)
            # whenever a task is found in the database we may have to redo all child artifacts because the children
            # will not be loaded
            if self.recompute_artifacts:
                logger.info(
                    f"Task task_id: {self.id} recomputing artifacts for task: {self.name}"
                )
                from simstack.core.recompute_artifacts import recompute_artifacts

                await recompute_artifacts(self.registry_entry)
            else:
                logger.warning(
                    f"Task task_id: {self.id} was found in the database with status: {self.registry_entry.status}. Terminating execution."
                )

        return self.registry_entry.status

    async def load_results(self) -> Union[Model, SimstackResult, None]:
        """
        Loads the results associated with a specific task from the database. This
        method verifies whether the task has valid result identifiers. If valid
        identifiers (results_id and results_table_name) exist, it attempts to fetch
        the outputs.

        If the task status is not TaskStatus.COMPLETED None is returned which results in a RuntimeError
        If the results are not found or if the retrieval process fails, a `ValueError` is raised.

        :raises ValueError: If the task has completed but lacks output identifiers
            (`results_id` or `results_table_name`), or if there is any error
            during the process of loading the results.

        :return: The retrieved task outputs from the database.
        """
        engine = current_engine_context.get()
        logger.info(f"Task task_id: {self.id} loading results {self.status}")
        try:
            if self.registry_entry.status != TaskStatus.COMPLETED:
                return None
            if len(self.registry_entry.result_tables) == 0:
                logger.warning(f"Task task_id: {self.id} completed but has no outputs")
            if len(self.registry_entry.result_tables) != len(
                self.registry_entry.result_ids
            ):
                raise ValueError(f"Task task_id: {self.id} has inconsistent results")
            simstack_result = SimstackResult(status=self.registry_entry.status)
            result = None
            for result_name, table_name, table_id in zip(
                self.registry_entry.result_names,
                self.registry_entry.result_tables,
                self.registry_entry.result_ids,
            ):
                model = await import_class(table_name)
                result = await engine.find_one(model, model.id == table_id)
                if result is None:
                    await self.set_status(TaskStatus.FAILED)
                    logger.error(
                        f"Task task_id: {self.id} could not find result with id {table_id} in table {table_name}"
                    )
                    raise ValueError(
                        f"Task task_id: {self.id} could not find result with id {table_id} in table {table_name}"
                    )
                simstack_result.__setattr__(result_name, result)

            logger.info(f"Task task_id: {self.id} loaded outputs")

            if len(self.registry_entry.result_tables) == 1:
                return result  # there is only one result, return it directly
            else:
                return simstack_result  # return the SimstackResult with all results

        except Exception as e:
            await self.set_status(TaskStatus.FAILED)
            logger.exception(f"Task task_id: {self.id} failed to load outputs: {e}")
            raise ValueError(f"Task task_id: {self.id} failed to load outputs: {e}")

    async def run_somewhere(self) -> Union[Model, SimstackResult, None]:
        """
        Executes the task either locally or on a remote resource. This function ensures that
        if the task is meant to execute on a remote resource, it waits for the task to complete
        remotely and fetches its results. If the task executes locally, it directly runs the task
        and retrieves the results.

        If any exception occurs during the execution, the status is updated to `FAILED`
        and the exception is logged.

        :return: A single Model or a list of Model instances of the task results or None. If the task
                 is not completed successfully.
        :rtype: Model | SimstackResult

        :raises RunTimeError: When task execution fails due to an unexpected exception.
        """
        resource_self = Resource(value="self")

        logger.info(
            f"Task task_id: {self.id} run_somewhere context resource: {context.config.resource} target resource: {self.parameters.resource}"
        )
        if (
            self.parameters.resource == resource_self
            or context.config.resource == self.parameters.resource
        ):
            result = await self.execute_node_locally()
            return result
        else:
            # the task will be executed somewhere else
            # wait for the database status to change
            while True:
                new_registry_entry = await context.db.load_task_by_id(self.id)
                # TODO add timeout mechanism here
                new_status = new_registry_entry.status
                if (
                    new_status != TaskStatus.RUNNING
                    and new_status != TaskStatus.SUBMITTED
                    and new_status != TaskStatus.SLURM_QUEUED
                    and new_status != TaskStatus.SLURM_RUNNING
                ):
                    break

                print(f"Task task_id: {self.id} is waiting for results")
                await asyncio.sleep(5)

            if new_status == TaskStatus.COMPLETED:
                logger.info(f"Task task_id: {self.id} completed remotely")
                self.registry_entry = new_registry_entry
                return await self.load_results()
            else:
                return None

    async def execute_node_locally(self) -> Union[Model, SimstackResult, None]:
        """
        Executes a specified node in the current context locally, either asynchronously or
        synchronously, managing task status updates, directory changes, and result persistence.

        This method handles the execution of a computational task represented as a "node". It
        manages the task's status transitions, file system operations for managing working
        directories, and handling output results, including their persistence in a database.
        The method supports both asynchronous and synchronous node execution. It verifies
        results, handles exceptions, and manages task metadata updates.

        Nodes can either return
              * a single `Model` instance
              * a `SimstackResult` instance
              * None (for failure).
              * a boolean value (for failure or success if there are no results)

        There is a try-except block around the actual execution of the node which generates a log entry
        "node function error for node" that catches all uncaught exceptions within the node.  These error are
        not propagated, but the task status is set to TaskStatus.FAILED.

        :param self: Instance of the class invoking this method.

        :raises Exception: for failures of the Simstack logic

        :return: The processed result of the node execution. Depending on the task's output,
                 it could be of the type `Model`, `SimstackResult`, or be None if no valid result
                 was produced.

        """
        self.registry_entry.started_at = datetime.now()
        await self.set_status(TaskStatus.RUNNING)
        logger.info(f"Task task_id: {self.id} is started on {self.parameters.resource}")
        original_dir = Path.cwd()
        try:
            node_runner = NodeRunner(self._func.__name__, None, task_id=self.id)
            node_kwargs = {
                "node_runner": node_runner,
                "parent_id": self.id,
                "task_id": self.id,
                "call_path": self.call_path,
                "parent_parameters": self.parameters,  # this must have a name different from parameters, because
                # otherwise this setting will override all the parameters of
                # the child nodes
                "recompute_artifacts": self.recompute_artifacts,
                "custom_name": self.custom_name,
            }

            if self.parameters.force_rerun:
                node_kwargs["force_rerun"] = True

            path = Path(context.config.workdir) / self.name / str(self.id)
            # Create the directory if it doesn't exist
            path.mkdir(parents=True, exist_ok=True)
            os.chdir(path)
            logger.info(
                f"Task task_id: {self.id} successfully changed to directory: {path.absolute()}"
            )

            # real_func_wrapper = await import_function_by_name(self._func.__name__,self.id)
            # real_func = getattr(real_func_wrapper, '_inner', real_func_wrapper)
            real_func = self._func
            result = None
            set_task_id(self.registry_entry.id)
            try:
                if self.is_async:
                    result = await real_func(*self._args, **node_kwargs)
                else:
                    result = real_func(*self._args, **node_kwargs)
            except Exception as e:
                logger.exception(
                    f"Task task_id: {self.id} node function error for node: {self.name} msg: {str(e)}"
                )
                # save what we can, in particular the info_files
                await self.process_results(node_runner)
                await self.set_status(TaskStatus.FAILED)
                raise
            finally:
                clear_task_id()

            self.registry_entry.completed_at = datetime.now()

            new_task_status, result = await self.process_results(result)

            if new_task_status == TaskStatus.COMPLETED:
                artifact_arguments = ArtifactArguments(result, self.id)
                artifact_arguments.add_attributes(
                    self._func, *self._args, **node_kwargs
                )
                self.registry_entry.artifact_ids = await create_artifacts(
                    artifact_arguments, self.registry_entry
                )
            await self.set_status(
                new_task_status
            )  # this will also commit the registry entry

            logger.info(
                f"Task task_id: {self.id} is finished on resource: {self.parameters.resource} with task status: {new_task_status}"
            )
            # code in 'finally' will be executed anyway
            return result
        except Exception:
            await self.set_status(TaskStatus.FAILED)
            raise
        finally:
            os.chdir(original_dir)
            logger.debug(
                f"Task task_id: {self.id} successfully back to directory: {original_dir.absolute()}"
            )

    async def process_results(self, result):
        # each of the following if sets the result either to a valid value or None
        new_task_status = TaskStatus.COMPLETED
        if result is None:
            logger.warning(f"Task task_id: {self.id} returned None")
            new_task_status = TaskStatus.FAILED  # result is None
        elif isinstance(result, bool):
            if not result:
                new_task_status = TaskStatus.FAILED
                result = None
        elif is_simstack_model(result):  # backward compatibility
            result_model = await context.db.upsert(result)
            result_table_name = await context.db.find_one(
                ModelMapping, ModelMapping.name == result.__class__.__name__
            )
            if result_table_name is None:
                logger.error(
                    f"Task task_id: {self.id} could not find table name for {result.__class__.__name__}"
                )
                raise ValueError(
                    f"Could not find table name for {result.__class__.__name__}"
                )
            self.registry_entry.result_ids = [result_model.id]
            self.registry_entry.result_tables = [result_table_name.mapping]
            self.registry_entry.result_names = [result.__class__.__name__]
            if hasattr(result, "task_status"):
                new_task_status = result.task_status
        elif isinstance(result, SimstackResult):
            # it is possible that the task failed internally but returned logging info which we process anyway
            new_task_status = result.status
            result_ids = []
            result_tables = []
            result_models = []
            result_names = []

            if hasattr(result, "custom_name"):
                self.registry_entry.custom_name = result.custom_name

            # check if there are files in the result
            if len(result.files) > 0:
                file_list_model = (
                    FileListModel()
                )  # this goes into the results must be a model
                for file_stack in result.files:
                    if file_stack:
                        if isinstance(file_stack, FileStack):
                            logger.info(
                                f"Task task_id: {self.id} saving file: {file_stack.name} {file_stack.id}"
                            )
                            saved = await context.db.save(file_stack)
                            file_list_model.append(saved)
                        else:
                            logger.error(
                                f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                            )
                            raise ValueError(
                                f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                            )
                    else:
                        logger.error(f"Task task_id: {self.id} saving file is NONE")
                        raise ValueError("saving file is NONE")

                result_table_name = await context.db.find_one(
                    ModelMapping, ModelMapping.name == FileListModel.__name__
                )
                if result_table_name is None:
                    logger.error(
                        f"Task task_id: {self.id} could not find table name for {FileListModel.__name__}"
                    )
                    raise ValueError(
                        f"Could not find table name for {FileListModel.__name__}"
                    )
                result_tables.append(result_table_name.mapping)
                result_names.append("files")
                saved = await context.db.save(file_list_model)
                result_ids.append(saved.id)
                result_models.append(saved)

            for file_stack in result.info_files:
                if file_stack:
                    if isinstance(file_stack, FileStack):
                        saved = await context.db.save(file_stack)
                        logger.info(
                            f"Task task_id: {self.id} saving info file: {file_stack.name} {file_stack.id}"
                        )
                        self.registry_entry.info_files.append(saved)
                    else:
                        logger.error(
                            f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                        )
                else:
                    logger.error(f"Task task_id: {self.id} saving info-file is NONE")
                    raise ValueError("saving info file is NONE")

            for key, value in getattr(result, "__pydantic_extra__", {}).items():
                if not callable(value) and is_simstack_model(value):
                    if isinstance(value, Model):
                        result_model = await context.db.upsert(value)
                        result_models.append(result_model)
                        result_ids.append(result_model.id)
                        result_names.append(key)
                        result_table_name = await context.db.find_one(
                            ModelMapping, ModelMapping.name == value.__class__.__name__
                        )
                        if result_table_name is None:
                            logger.error(
                                f"Task task_id: {self.id} could not find table name for {value.__class__.__name__}"
                            )
                            raise ValueError(
                                f"Could not find table name for {value.__class__.__name__}"
                            )
                        result_tables.append(result_table_name.mapping)
                    else:
                        raise ValueError(
                            f"task_id: {self.id} cannot save model: {key} is not a model"
                        )

            self.registry_entry.result_ids = result_ids
            self.registry_entry.result_tables = result_tables
            self.registry_entry.result_names = result_names

            if result.error_message is not None and result.error_message != "":
                logger.error(
                    f"Task task_id: {self.id} returned with error: {result.error_message}"
                )
            if result.message is not None and result.message != "":
                logger.info(f"Task task_id: {self.id} message: {result.message}")
            if len(result_ids) == 1:
                result = result_models[
                    0
                ]  # this is a SimstackResult with just one returned model
        return new_task_status, result

    async def set_status(self, status: TaskStatus):
        if self.registry_entry is None:
            raise ValueError("Task has no registry entry")
        if isinstance(status, TaskStatus):
            self.registry_entry.status = status
        else:
            logger.warning(f"Task task_id: {self.id} {status} is not a TaskStatus")
            self.registry_entry.status = TaskStatus(status)
        engine = current_engine_context.get()
        await engine.save(self.registry_entry)
        logger.info(f"Task task_id: {self.id} is set to {status}")


async def node_from_database(registry_entry: NodeRegistry) -> Union["Node", None]:
    """
    Constructs an instance of the class from database information encoded in a
    registry entry.

    This method retrieves input arguments and the serialized function from the
    database using information provided in the `registry_entry`. It then deserializes
    the function and initializes a corresponding Node instance, associating it
    with the given registry entry.

    This function can delete the registry_entry !!!
    The only way that registry_entry.function_hash is "NOT INITIALIZED" is when the node
    is created from the frontend. No other node is listening specifically for this registry_entry to complete.
    If a duplicate is found the node from the duplication is returned

    :param registry_entry: The registry entry containing information necessary to
        reconstruct the Node instance. Includes input table names, function pickled
        as a string, and other metadata.
    :type registry_entry: NodeRegistry

    :return: A reconstructed Node instance based on the registry entry, or None if
        the deserialized function is not valid or there was an error.
    :rtype: Optional[Node]
    """
    args = []
    engine = current_engine_context.get()

    for table, table_id in zip(registry_entry.input_tables, registry_entry.input_ids):
        try:
            model = await import_class(table)
            arg = await engine.find_one(model, model.id == table_id)
            args.append(arg)
        except Exception as e:
            logger.exception(
                f"Task task_id: {registry_entry.id} failed to load input {table} with id {table_id}: {str(e)}"
            )
            return None

    if registry_entry.arg_hash == "NOT INITIALIZED":
        logger.debug(f"Task task_id: {registry_entry.id} computes arg hashes")
        registry_entry.arg_hash = compute_arg_hash(args)

    logger.debug(f"Task task_id: {registry_entry.id} loaded {len(args)} inputs")
    try:
        wrapped_func = await import_function(
            registry_entry.func_mapping, registry_entry.id
        )
        if wrapped_func is None:
            logger.error(f"Task task_id: {registry_entry.id} could not import function {registry_entry.func_mapping}")
            return None
        # for nodes the mapping points to the wrapped func to we use that
        func = (wrapped_func if not hasattr(wrapped_func, "_inner") else wrapped_func._inner)
        logger.info(
            f"Task task_id: {registry_entry.id} inner: {hasattr(wrapped_func, '_inner')} imported function: {func.__name__}"
        )
        if registry_entry.function_hash == "NOT INITIALIZED":
            registry_entry.function_hash = complex_hash_function(func)
            registry_entry.is_async = asyncio.iscoroutinefunction(func)
            duplicate_entry = await engine.find_one(
                NodeRegistry,
                (NodeRegistry.name == registry_entry.name)
                & (NodeRegistry.arg_hash == registry_entry.arg_hash)
                & (NodeRegistry.function_hash == registry_entry.function_hash))
            await engine.save(registry_entry)  # save the fixed entry AFTER checking for duplicates
            # the calling function may have the originial entry unsaved !
            if duplicate_entry is not None:
                logger.info(f"Original Entry: {duplicate_entry.id} {duplicate_entry.arg_hash} {duplicate_entry.function_hash}")
                logger.info(f"Current Entry: {registry_entry.id} {registry_entry.arg_hash} {registry_entry.function_hash} ")
                logger.info(f"Task task_id: {registry_entry.id} found duplicate entry {duplicate_entry.id} {duplicate_entry.name}")
                if duplicate_entry.id == registry_entry.id:
                    logger.error(f"Task task_id: {registry_entry.id} recovered itself. This should not happen")

                if duplicate_entry.id != registry_entry.id:
                    await engine.delete(registry_entry)
                    registry_entry = duplicate_entry

    except Exception as e:
        logger.exception(
            f"Task task_id: {registry_entry.id} failed to import function {registry_entry.func_mapping} {str(e)}"
        )
        return None

    kwargs = {
        "func": func,
        "is_async": False,
        "call_path": registry_entry.call_path,
        "parameters": registry_entry.parameters,
        "custom_name": registry_entry.custom_name,
    }
    if hasattr(registry_entry, "is_async"):
        kwargs["is_async"] = registry_entry.is_async

    kwargs["parent_id"] = (
        registry_entry.parent_ids[0] if registry_entry.parent_ids else None
    )
    logger.info(
        f"Task task_id: {registry_entry.id} is_async: {kwargs['is_async']} parent_id: {kwargs['parent_id']}"
    )

    new_node = Node(*args, **kwargs)
    new_node.registry_entry = registry_entry
    return new_node


# Add a return type annotation for async functions
# T_co = TypeVar("T_co", covariant=True)
#
# # Create overloaded function type annotations
# @overload
# def node(
#     _func: Callable[..., Awaitable[T_co]],
# ) -> Callable[..., Awaitable[T_co]]: ...
#
# @overload
# def node(
#     _func: Callable[..., T_co],
# ) -> Callable[..., T_co]: ...
#
# @overload
# def node(
#     _func: None = None,
#     *,
#     name: Optional[str] = None,
#     version: Optional[str] = None,
#     cache: bool = True,
#     **kwargs_node,
# ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


P = ParamSpec("P")


@overload
def node(_func: Callable[P, T]) -> Callable[..., T]:
    ...


@overload
def node(
    _func: None = None,
    *,
    name: Optional[str] = None,
    version: Optional[str] = None,
    cache: bool = True,
    **kwargs_node,
) -> Callable[[Callable[P, T]], Callable[..., T]]:
    ...


def node(
    _func: Optional[Callable[P, T]] = None,
    *,
    version: Optional[str] = None,
    **kwargs_node,
) -> Union[Callable[..., T], Callable[[Callable[P, T]], Callable[..., T]]]:
    """
    Decorator to mark a function as a node in the computation graph.
    Supports both synchronous and asynchronous functions.
    Can be used with or without parameters:
    @node
    def func(): ...

    @node(name="example")
    def func(): ...

    """

    def decorator(func: Callable[P, T]) -> Callable[..., T]:
        is_async = asyncio.iscoroutinefunction(func)

        func._is_node = True
        func._inner = func
        func._node_parameters = kwargs_node.get("parameters", Parameters())

        def update_kwargs(kwargs):
            kwargs["func"] = func
            kwargs["is_async"] = is_async
            kwargs["parameters"] = kwargs.pop(
                "parameters", kwargs_node.get("parameters", Parameters())
            )
            kwargs["custom_name"] = kwargs_node.get(
                "custom_name", default_name_generator()
            )
            call_path = kwargs.pop("call_path", "")
            if not call_path:
                call_path = ""
            logger.debug(f"Task call_path: {call_path} {func.__name__}")
            # Fix call path construction - handle empty call_path for root nodes
            if call_path == "":
                call_path = "." + func.__name__
            else:
                call_path = call_path + "." + func.__name__
            kwargs["call_path"] = call_path

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            update_kwargs(kwargs)
            # Create Node with the remaining kwargs
            execution_node = Node(*args, **kwargs)

            status = await execution_node.get_node_registry()
            result = None
            if status == TaskStatus.COMPLETED:
                result = await execution_node.load_results()
            elif status == TaskStatus.SUBMITTED:
                result = await execution_node.run_somewhere()
            if result is None or execution_node.status != TaskStatus.COMPLETED:
                raise RuntimeError(
                    f"Task task_id: {execution_node.id} node: {execution_node.name} terminated with status {execution_node.status}"
                )
            return result

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            import asyncio

            update_kwargs(kwargs)
            # Create Node with the remaining kwargs
            execution_node = Node(*args, **kwargs)

            # If it's an async function but called in a sync context, run it in the event loop
            loop = asyncio.get_event_loop()
            status = loop.run_until_complete(execution_node.get_node_registry())
            result = None
            if status == TaskStatus.COMPLETED:
                return loop.run_until_complete(execution_node.load_results())
            elif status == TaskStatus.SUBMITTED:
                return loop.run_until_complete(execution_node.run_somewhere())
            if result is None or execution_node.status != TaskStatus.COMPLETED:
                raise RuntimeError(
                    f"Task task_id: {execution_node.id} node: {execution_node.name} terminated with status {execution_node.status}"
                )
            return result

        async_wrapper.is_node = True
        sync_wrapper.is_node = True
        # Return the appropriate wrapper based on whether the function is async
        if is_async:
            return cast(Callable[..., T], async_wrapper)
        else:
            return cast(Callable[..., T], sync_wrapper)

    decorator.is_node = True

    if _func is None:
        # Called with parameters: @node(...)
        return decorator
    else:
        # Called without parameters: @node
        return decorator(_func)
