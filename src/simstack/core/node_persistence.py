import asyncio
import copy
import inspect
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    TypeVar,
    cast,
    List,
    Union,
)

import coolname  # type: ignore[import-untyped]
import nest_asyncio  # type: ignore[import-untyped]
from simstack.core.context import context
from simstack.runner_protocol.constants import (
    RUNNER_CHILD_PENDING_STATUSES,
    RunnerTaskStatus as TaskStatus,
)
from simstack.core.hash import complex_hash_function
from simstack.core.node_code_version import node_code_version
from simstack.domain.execution_parameters import inherit_parent_slurm_parameters_for_self
from simstack.domain.input_hashing import hashable_inputs
from simstack.core.node_runner import NodeRunner
from simstack.core.simstack_result import SimstackResult
from simstack.core.task_id import set_task_id, clear_task_id
from simstack.models import Parameters, Project
from simstack.models import NodeRegistry, NamedDataReference
from simstack.models.document import Model, ObjectId
from simstack.persistence import Repository, all_of, eq
from simstack.models.file_list import FileList
from simstack.models.files import FileStack
from simstack.models.parameters import Resource, Queue
from simstack.models.simstack_model import is_simstack_model

logger = logging.getLogger("Node")

nest_asyncio.apply()

T = TypeVar("T")


def default_name_generator() -> str:
    return str("-".join(coolname.generate(2)))


def compute_arg_hash(args: List[Model]) -> str:
    """
    Computes a hash for a list of arguments provided, where each argument
    is an instance of the Model class or can be processed into a hashable
    format. Uses a complex hashing function for the resulting computation.

    Args:
        args (List[Model]): A list of objects where each object must be an instance of the
            Model class. The objects are used to compute their respective
            hash values via a specified complex hashing mechanism.

    Returns:
        str: A string representation of the computed hash for the provided
            list of arguments.

    Raises:
        TypeError: If any item in the provided list is not an instance of the
            Model class.
    """
    arg_hashes = []
    for arg in args:
        if isinstance(arg, Model):
            arg_hash = (
                arg.complex_hash()
                if hasattr(arg, "complex_hash")
                else complex_hash_function(hashable_inputs(arg))
            )
            arg_hashes.append(arg_hash)
        else:
            raise TypeError(f"Argument {arg} is not an instance of {Model}")
    return cast(str, complex_hash_function(arg_hashes))


def _parameters_field_values(parameters: Parameters) -> dict[str, Any]:
    raw_values = object.__getattribute__(parameters, "__dict__")
    return {
        field_name: raw_values[field_name]
        for field_name in Parameters.model_fields
        if field_name in raw_values
    }


def _parameters_from_node_kwargs(kwargs_node: dict[str, Any]) -> Parameters:
    base_parameters = kwargs_node.get("parameters")
    if isinstance(base_parameters, Parameters):
        values = _parameters_field_values(base_parameters)
    elif base_parameters is None:
        values = _parameters_field_values(Parameters())
    else:
        values = _parameters_field_values(Parameters.model_validate(base_parameters))

    for field_name in Parameters.model_fields:
        if field_name in kwargs_node:
            values[field_name] = kwargs_node[field_name]

    return Parameters.model_validate(values)


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
    """

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__()

        self._args = list(args)  # Convert to list to allow appending

        # Extract specific known parameters
        self.repository: Repository = kwargs.pop("repository")
        self._func = kwargs.pop("func")
        self.name = self._func.__name__
        self.is_async = kwargs.pop("is_async")
        self.parent_id = kwargs.pop("parent_id", None)
        self.call_path = kwargs.pop("call_path", "") or "." + self.name
        self._arg_hash = kwargs.pop("arg_hash", None)
        self._function_hash = kwargs.pop("function_hash", None)

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
        self.registry_entry: NodeRegistry | None = None
        self._registry_snapshot: dict[str, Any] | None = None
        self._result_custom_name: str | None = None

    def _remember_registry(self) -> None:
        assert self.registry_entry is not None
        self._registry_snapshot = copy.deepcopy(self.registry_entry.model_dump(mode="python"))

    async def _persist_execution_changes(self) -> None:
        """Save this execution's changes without replacing concurrent UI edits."""
        assert self.registry_entry is not None
        if self._registry_snapshot is None:
            persisted = await self.repository.find_one(NodeRegistry, eq("id", self.id))
            if persisted is None:
                raise RuntimeError(f"Task {self.id} no longer exists")
            self._registry_snapshot = persisted.model_dump(mode="python")
        snapshot = self._registry_snapshot
        payload = self.registry_entry.model_dump(mode="python")
        execution_fields = {
            "status", "started_at", "completed_at", "job_id", "results_references",
            "info_files", "artifact_ids", "error", "message", "return_kind",
        }
        changes = {
            field: payload[field] for field in execution_fields
            if payload[field] != snapshot[field]
        }
        expected = {field: snapshot[field] for field in ("status", "started_at")}
        if self._result_custom_name is not None:
            changes["custom_name"] = self._result_custom_name
            expected["custom_name"] = snapshot["custom_name"]
        if not changes:
            return
        updated = await self.repository.update_node_registry(self.id, changes, expected=expected)
        if updated is None and "custom_name" in changes:
            # A user rename wins, while the result and terminal status can
            # still be committed under the same execution-state fence.
            changes.pop("custom_name")
            expected.pop("custom_name")
            updated = await self.repository.update_node_registry(self.id, changes, expected=expected)
        if updated is None:
            raise RuntimeError(f"Task {self.id} execution state changed concurrently")
        self.registry_entry = updated
        self._result_custom_name = None
        self._remember_registry()

    def _apply_parent_slurm_for_self_resource(self) -> bool:
        previous = self.parameters.slurm_parameters
        inherit_parent_slurm_parameters_for_self(
            self.parameters, self._function_kwargs.get("parent_parameters")
        )
        if self.registry_entry is not None:
            self.registry_entry.parameters = self.parameters
        return self.parameters.slurm_parameters is not previous

    @property
    def id(self) -> ObjectId | None:
        if self.registry_entry is None:
            return None
        else:
            return self.registry_entry.id

    @property
    def status(self) -> TaskStatus:
        return getattr(self.registry_entry, "status", TaskStatus.FAILED)

    async def make_registry_entry(
        self, function_hash: str, arg_hash: str
    ) -> NodeRegistry:
        """
        Creates a registry entry for the node in the database.

        This method is used to create a new entry in the database for the node,
        including its inputs and outputs. It ensures that the task is properly
        registered with all necessary details.

        :rtype: NodeRegistry
        """
        # TODO why does this fail when nodemapping succeeds ?
        function_mapping = context.node_mappings.get_by_name(self.name)
        if function_mapping is None:
            logger.error(f"Could not find function mapping for name: {self.name}")
            raise ValueError(f"Could not find function mapping for name: {self.name}")

        input_references = []
        # Get function signature to identify argument names
        sig = inspect.signature(self._func)
        param_names = list(sig.parameters.keys())

        for i, arg in enumerate(self._args):
            # if there is no table for an arg raise an error
            input_table_name = context.model_mappings.get_by_name(arg.__class__.__name__)
            if input_table_name is None:
                logger.error(f"Could not find table name for {arg.__class__.__name__}")
                raise ValueError(f"Could not find table name for {arg.__class__.__name__}")
            if not isinstance(arg, Model):
                logger.error(f"{arg.__class__.__name__} is not a SimStack document model")
                raise ValueError(
                    f"{arg.__class__.__name__} is not a SimStack document model"
                )

            argument_entry = await self.repository.save(arg)

            # Check if the save operation was successful and returned a valid ID
            if argument_entry is None or argument_entry.id is None:
                logger.error(f"Failed to save argument {arg} - returned None or invalid ID")
                raise ValueError(f"Failed to save argument of type {arg.__class__.__name__}")

            variable_name = param_names[i] if i < len(param_names) else f"arg_{i}"

            input_references.append(NamedDataReference.from_variable(
                argument_entry,
                variable_name=variable_name,
                task_id=str(self.id)
            ))

        delayed_message = "" # there is no task_id yet
        if self.parent_id is None:
            projects = await self.repository.find(Project)
            if projects is None or len(projects) == 0:
                project = Project(field_name="default")
                await self.repository.save(project)
                project_id = project.id
                delayed_message = f"default project: {project_id} "
            else:
                project = projects[0]
                project_id = project.id
                delayed_message = f"project: {project_id} "
        else:
            parent_registry_entry = await self.repository.find_one(
                NodeRegistry, eq("id", self.parent_id)
            )
            if parent_registry_entry is None:
                raise ValueError(f"Parent task {self.parent_id} does not exist")
            project_id = parent_registry_entry.project
            delayed_message = f"using parent project: {project_id} "

        self.registry_entry = NodeRegistry(
            name=self.name,
            input_references=input_references,
            is_async=self.is_async,
            status=TaskStatus.SUBMITTED,
            custom_name=self.custom_name,
            version=getattr(self._func, "_node_version", None),
            function_hash=function_hash,
            arg_hash=arg_hash,
            project=project_id,
            parent_ids=[] if self.parent_id is None else [self.parent_id],
            parameters=self.parameters,
            func_mapping=function_mapping.function_mapping,
            call_path=self.call_path,
        )

        if delayed_message:
            logger.info(f"Task task_id: {self.id} with name {self.name} {delayed_message}")
        parent_parameters = self._function_kwargs.get("parent_parameters", None)
        registry_entry = self.registry_entry
        assert registry_entry is not None
        from simstack.core.resource_assignment import (
            apply_resource_assignment_to_node_registry,
        )

        await apply_resource_assignment_to_node_registry(
            self.repository,
            registry_entry,
            parent_parameters=parent_parameters
            if isinstance(parent_parameters, Parameters)
            else None,
        )
        self.parameters = registry_entry.parameters
        self._apply_parent_slurm_for_self_resource()
        await self.repository.save(registry_entry)
        self._remember_registry()
        logger.info(
            f"Task task_id: {self.id} with name {self.name} created for resource: {registry_entry.parameters.resource} queue: {registry_entry.parameters.queue} with id: {self.id}"
        )
        return registry_entry

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
        arg_hash = compute_arg_hash(self._args)
        function_hash = node_code_version(self._func)
        self._arg_hash = arg_hash
        self._function_hash = function_hash

        from simstack.core.resource_assignment import resolve_resource_assignment

        resolution = await resolve_resource_assignment(
            self.repository, call_path=self.call_path,
            base_parameters=self.parameters,
            parent_parameters=self._function_kwargs.get("parent_parameters"),
        )
        self.parameters = resolution.parameters
        self._apply_parent_slurm_for_self_resource()

        self.registry_entry = None
        if not self.parameters.force_rerun:
            candidates = await self.repository.find(
                NodeRegistry,
                all_of(
                    eq("name", self.name),
                    eq("arg_hash", arg_hash),
                    eq("function_hash", function_hash),
                ),
            )
            route_fields = ("resource", "queue", "in_docker", "slurm_parameters")
            route = self.parameters.model_dump(mode="python", include=set(route_fields))
            self.registry_entry = next(
                (candidate for candidate in candidates if candidate.status == TaskStatus.COMPLETED),
                None,
            )
            if self.registry_entry is None:
                self.registry_entry = next(
                    (candidate for candidate in candidates
                     if candidate.parameters.model_dump(mode="python", include=set(route_fields)) == route),
                    None,
                )

        if self.registry_entry is None:
            await self.make_registry_entry(function_hash, arg_hash)
        else:

            if self.parent_id:
                logger.debug(
                    f"Task task_id: {self.id} adding parent_id {self.parent_id} to task: {self.name}"
                )
                if isinstance(self.parent_id, str):
                    logger.error(
                        f"Task task_id: {self.id} parent_id is a string: {self.parent_id}"
                    )
                    self.parent_id = ObjectId(self.parent_id)
                updated = await self.repository.add_node_parent(self.id, self.parent_id)
                if updated is None:
                    raise RuntimeError(f"Task {self.id} no longer exists")
                self.registry_entry = updated
            self._remember_registry()
            # whenever a task is found in the database, we may have to redo all child artifacts because the children
            # will not be loaded
            if self.recompute_artifacts:
                logger.debug(
                    f"Task task_id: {self.id} recomputing artifacts for task: {self.name}"
                )
                from simstack.core.recompute_artifacts import recompute_artifacts

                await recompute_artifacts(self.registry_entry, self.repository)
            else:
                logger.warning(
                    f"Task task_id: {self.id} was found in the database with status: {self.registry_entry.status}. Terminating execution."
                )

        assert self.registry_entry is not None
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
        from simstack.util.importer import import_class

        repository = self.repository
        assert self.registry_entry is not None
        logger.info(
            f"Task task_id: {self.id} loading results with task status {self.status}"
        )
        try:
            if self.registry_entry.status != TaskStatus.COMPLETED:
                return None
            if self.registry_entry.return_kind == "bool":
                return True

            simstack_result = SimstackResult.from_task_metadata(
                self.registry_entry.model_dump(mode="python"),
                info_files=list(self.registry_entry.info_files),
            )
            result = None
            for ref in self.registry_entry.results_references:
                model = await import_class(ref.variable_mapping, repository)
                result = await repository.find_one(model, eq("id", ref.reference))
                if result is None:
                    await self.set_status(TaskStatus.FAILED)
                    logger.error(
                        f"Task task_id: {self.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                    )
                    raise ValueError(
                        f"Task task_id: {self.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                    )
                simstack_result.__setattr__(ref.variable_name, result)

            logger.info(f"Task task_id: {self.id} loaded outputs")

            if len(self.registry_entry.results_references) == 1 and self.registry_entry.return_kind != "multiple":
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
            f"Task task_id: {self.id} run_somewhere context resource: {context.config.resource} target resource: {self.parameters.resource} queue: {self.parameters.queue}"
        )
        if self.parameters.resource == resource_self or (
            context.config.resource == self.parameters.resource
            and self.parameters.queue == Queue.DEFAULT
        ):
            result = await self.execute_node_locally()
            return result
        else:
            # the task will be executed somewhere else
            # wait for the database status to change
            while True:
                new_registry_entry = await self.repository.find_one(
                    NodeRegistry, eq("id", self.id)
                )
                # TODO add timeout mechanism here
                if new_registry_entry is None:
                    raise RuntimeError(
                        f"Task task_id: {self.id} could not be found in the database"
                    )
                new_status = new_registry_entry.status
                if new_status not in RUNNER_CHILD_PENDING_STATUSES:
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
        assert self.registry_entry is not None
        self.registry_entry.started_at = datetime.now()
        await self.set_status(TaskStatus.RUNNING)
        logger.info(
            f"Task task_id: {self.id} is started on {self.parameters.resource} in Node:execute_node_locally"
        )
        original_dir = Path.cwd()
        try:
            node_runner = NodeRunner(self._func.__name__, self.id)
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
                "arg_hash": self._arg_hash,
                "function_hash": self._function_hash,
            }

            if self.parameters.force_rerun:
                node_kwargs["force_rerun"] = True

            path = Path(context.config.workdir) / self.name / str(self.id)
            # Create the directory if it doesn't exist
            path.mkdir(parents=True, exist_ok=True)
            os.chdir(path)
            logger.debug(
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
                # Save the error message if possible
                if self.registry_entry:
                    self.registry_entry.error = str(e)
                    await self._persist_execution_changes()
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
                from simstack.core.artifacts import ArtifactArguments, create_artifacts

                artifact_arguments = ArtifactArguments(result, self.id)
                artifact_arguments.add_attributes(
                    self._func, *self._args, **node_kwargs
                )
                self.registry_entry.artifact_ids = await create_artifacts(
                    artifact_arguments,
                    self.registry_entry,
                    self.repository,
                )
            await self.set_status(
                new_task_status
            )  # this will also commit the registry entry

            logger.info(
                f"Task task_id: {self.id} is finished on resource: {self.parameters.resource} with task status: {new_task_status}"
            )
            if new_task_status != TaskStatus.COMPLETED:
                return None
            return result
        except Exception:
            await self.set_status(TaskStatus.FAILED)
            raise
        finally:
            os.chdir(original_dir)
            logger.debug(
                f"Task task_id: {self.id} successfully back to directory: {original_dir.absolute()}"
            )

    async def process_results(self, result: Any) -> tuple[TaskStatus, Any]:
        assert self.registry_entry is not None
        # each of the following if sets the result either to a valid value or None
        new_task_status = TaskStatus.COMPLETED
        self.registry_entry.return_kind = (
            "bool" if isinstance(result, bool) else "multiple" if isinstance(result, SimstackResult)
            else "model" if isinstance(result, Model) else "none"
        )
        if result is None:
            logger.warning(f"Task task_id: {self.id} returned None")
            new_task_status = TaskStatus.FAILED  # result is None
        elif isinstance(result, bool):
            if not result:
                new_task_status = TaskStatus.FAILED
                self.registry_entry.error = "Node returned False"
                result = None
        elif is_simstack_model(result) or isinstance(result, SimstackResult):
            if isinstance(result, SimstackResult):
                new_task_status = result.status
                if result.custom_name is not None:
                    self.registry_entry.custom_name = result.custom_name
                    self._result_custom_name = result.custom_name

                for file_stack in result.info_files:
                    if file_stack:
                        if isinstance(file_stack, FileStack):
                            logger.info(
                                f"Task task_id: {self.id} saving info file: {file_stack.name} {file_stack.id}"
                            )
                            if self.registry_entry.info_files is None:
                                self.registry_entry.info_files = FileList()
                            await self.repository.save(file_stack)
                            self.registry_entry.info_files.append(file_stack)
                        else:
                            logger.error(
                                f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                            )
                    else:
                        logger.error(f"Task task_id: {self.id} saving info-file is NONE")
                        raise ValueError("saving info file is NONE")

                if result.error_message is not None and result.error_message != "":
                    self.registry_entry.error = result.error_message
                    logger.error(
                        f"Task task_id: {self.id} returned with error: {result.error_message}"
                    )
                if result.message is not None and result.message != "":
                    self.registry_entry.message = result.message
                    logger.info(f"Task task_id: {self.id} message: {result.message}")
            else:
                if hasattr(result, "status"):
                    new_task_status = result.status
                elif hasattr(result, "task_status"):
                    new_task_status = result.task_status

            from simstack.core.process_results import process_result_helper

            results_references, result_models = await process_result_helper(
                result, str(self.id), self.repository
            )

            self.registry_entry.results_references = results_references
            self.registry_entry.status = new_task_status

            if len(results_references) == 1:
                result = result_models[0]  # for a SimstackResult with just one returned model we return the model directly
                self.registry_entry.return_kind = "model"
        else:
            logger.warning(
                f"Task task_id: {self.id} returned a result of type {type(result)} which is not a SimstackModel or a SimstackResult"
            )
            new_task_status = TaskStatus.FAILED
        return new_task_status, result

    async def set_status(self, status: TaskStatus) -> None:
        if self.registry_entry is None:
            raise ValueError("Task has no registry entry")
        if isinstance(status, TaskStatus):
            self.registry_entry.status = status
        else:
            logger.warning(f"Task task_id: {self.id} {status} is not a TaskStatus")
            self.registry_entry.status = TaskStatus(status)
        await self._persist_execution_changes()
        logger.info(f"Task task_id: {self.id} {self.name} is set to {status}, id is: {self.id}")


async def reconstruct_persisted_node(
    registry_entry: NodeRegistry,
    repository: Repository,
) -> "Node":
    """Strictly reconstruct a non-runner node through the repository port."""

    from simstack.util.importer import import_class, import_function

    args: list[Model] = []
    for reference in registry_entry.input_references:
        model_class = await import_class(reference.variable_mapping, repository)
        argument = await repository.find_one(
            model_class,
            eq("id", reference.reference),
        )
        if argument is None:
            raise ValueError(
                f"Task {registry_entry.id} references missing input "
                f"{reference.variable_mapping}:{reference.reference}"
            )
        args.append(argument)

    if registry_entry.arg_hash == "NOT INITIALIZED":
        raise ValueError(
            f"Task {registry_entry.id} has no initialized argument hash"
        )
    actual_arg_hash = compute_arg_hash(args)
    if actual_arg_hash != registry_entry.arg_hash:
        raise ValueError(
            f"Task {registry_entry.id} argument hash does not match persisted inputs"
        )

    wrapped_func = await import_function(
        registry_entry.func_mapping,
        repository,
        task_id=registry_entry.id,
    )
    if wrapped_func is None:
        raise ValueError(
            f"Task {registry_entry.id} function {registry_entry.func_mapping} "
            "could not be imported"
        )
    func = (
        wrapped_func
        if not hasattr(wrapped_func, "_inner")
        else wrapped_func._inner
    )
    if registry_entry.function_hash == "NOT INITIALIZED":
        raise ValueError(
            f"Task {registry_entry.id} has no initialized function hash"
        )
    actual_function_hash = node_code_version(func)
    if actual_function_hash != registry_entry.function_hash:
        raise ValueError(
            f"Task {registry_entry.id} function hash does not match imported code"
        )

    execution_node = Node(
        *args,
        repository=repository,
        func=func,
        is_async=registry_entry.is_async,
        call_path=registry_entry.call_path,
        parameters=registry_entry.parameters,
        custom_name=registry_entry.custom_name,
        arg_hash=registry_entry.arg_hash,
        function_hash=registry_entry.function_hash,
        parent_id=(
            registry_entry.parent_ids[0]
            if registry_entry.parent_ids
            else None
        ),
    )
    execution_node.registry_entry = registry_entry
    execution_node._remember_registry()
    return execution_node
class PersistentNodeExecutor:
    """Execute decorated nodes through an explicitly injected repository."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def execute_async(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        execution_node = Node(
            *args,
            repository=self._repository,
            **kwargs,
        )
        status = await execution_node.get_node_registry()
        result = None
        if status == TaskStatus.COMPLETED:
            result = await execution_node.load_results()
        elif status in {
            TaskStatus.SUBMITTED,
            TaskStatus.RETRIEVED,
            TaskStatus.SLURM_QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.SLURM_RUNNING,
        }:
            result = await execution_node.run_somewhere()

        if result is None or execution_node.status != TaskStatus.COMPLETED:
            if execution_node.registry_entry is None:
                raise RuntimeError(
                    f"Task {execution_node.id} has no registry entry"
                )
            current = await self._repository.find_one(
                NodeRegistry,
                eq("id", execution_node.registry_entry.id),
            )
            if current is None:
                raise RuntimeError(
                    f"Task {execution_node.id} registry entry disappeared"
                )
            if current.status == TaskStatus.FAILED and current.error:
                raise RuntimeError(current.error)
            raise RuntimeError(
                f"Task {current.id} terminated with status {current.status}"
            )
        return result

    def execute_sync(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        execution_node = Node(
            *args,
            repository=self._repository,
            **kwargs,
        )
        loop = asyncio.get_event_loop()
        status = loop.run_until_complete(execution_node.get_node_registry())
        result = None
        ran_somewhere = False
        if status == TaskStatus.COMPLETED:
            result = loop.run_until_complete(execution_node.load_results())
        elif status in {
            TaskStatus.SUBMITTED,
            TaskStatus.RETRIEVED,
            TaskStatus.SLURM_QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.SLURM_RUNNING,
        }:
            result = loop.run_until_complete(execution_node.run_somewhere())
            ran_somewhere = True
        if (
            ran_somewhere
            and result is None
            and execution_node.parameters.queue != Queue.SLURM_QUEUE
        ):
            if execution_node.registry_entry is not None and execution_node.registry_entry.error:
                raise RuntimeError(execution_node.registry_entry.error)
            return result
        if result is None or execution_node.status != TaskStatus.COMPLETED:
            if (
                execution_node.registry_entry is not None
                and execution_node.registry_entry.status == TaskStatus.FAILED
                and execution_node.registry_entry.error
            ):
                raise RuntimeError(execution_node.registry_entry.error)
            raise RuntimeError(
                f"Task {execution_node.id} terminated with status "
                f"{execution_node.status}"
            )
        return result


__all__ = ["Node", "PersistentNodeExecutor", "reconstruct_persisted_node"]
