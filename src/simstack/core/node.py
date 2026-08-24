import asyncio
import functools
import inspect
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Optional,
    TypeVar,
    cast,
    List,
    ParamSpec,
    Union,
    overload, Tuple,
)

import coolname  # type: ignore[import-untyped]
import nest_asyncio  # type: ignore[import-untyped]
from odmantic import Model, ObjectId
from pydantic import BaseModel

from simstack.core.artifacts import create_artifacts, ArtifactArguments
from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.hash import complex_hash_function
from simstack.core.node_claim import claim_submitted_node
from simstack.core.node_runner import NodeRunner
from simstack.core.process_results import process_result_helper
from simstack.core.resource_assignment import apply_resource_assignment_to_node_registry
from simstack.core.simstack_result import SimstackResult
from simstack.core.task_id import set_task_id, clear_task_id
from simstack.models import ModelMapping, Parameters, Project
from simstack.models import NodeModel
from simstack.models import NodeRegistry, NamedDataReference
from simstack.models.file_list import FileList
from simstack.models.files import FileStack
from simstack.models.parameters import Resource, Queue, SlurmParameters
from simstack.models.simstack_model import is_simstack_model
from simstack.util.importer import import_function, import_class

logger = logging.getLogger("Node")

nest_asyncio.apply()

T = TypeVar("T")

_DOCKER_HUB_LIBRARY_PREFIX = "docker.io/library/"
_DOCKERENV_PATH = Path("/.dockerenv")


def process_is_in_docker() -> bool:
    """True when this Python process is already running inside a container."""
    try:
        if context.in_docker:
            return True
    except RuntimeError:
        pass
    return _DOCKERENV_PATH.exists()


def normalize_docker_image(image: Optional[str]) -> Optional[str]:
    """Strip Hub library prefixes so equivalent image refs compare equal."""
    if not isinstance(image, str):
        return None
    name = image.strip()
    if not name:
        return None
    if name.startswith(_DOCKER_HUB_LIBRARY_PREFIX):
        return name[len(_DOCKER_HUB_LIBRARY_PREFIX) :]
    if name.startswith("docker.io/"):
        return name[len("docker.io/") :]
    return name


def docker_image_for_node(
    node_name: Optional[str], resource: Optional[object] = None
) -> Optional[str]:
    """Look up ``[resource.program.<node>].docker_image`` for a node."""
    if not node_name:
        return None
    try:
        resource_config = context.resource_config
        config = context.config
    except RuntimeError:
        return None
    if resource_config is None:
        return None
    if resource is not None:
        task_resource = str(resource)
    else:
        task_resource = str(config.resource)
    lookup_resource = "local" if task_resource == "self" else task_resource
    program_config = resource_config.get_program(node_name, resource=lookup_resource)
    if not program_config and lookup_resource != str(config.resource):
        program_config = resource_config.get_program(node_name)
    image = program_config.get("docker_image") if program_config else None
    if not isinstance(image, str) or not image.strip():
        return None
    return image


def should_dispatch_nested_docker(
    parameters: Parameters, node_name: str
) -> bool:
    """True when a nested child must wait for the host to start another image.

    Same resource + default queue stays in-process unless this process is
    already in Docker. While in a container, stay in-process only when both
    images are known and equal. A missing child image or a different image
    waits for the host runner (assignment ``in_docker`` is not required).
    """
    in_container = process_is_in_docker()
    assignment_in_docker = bool(getattr(parameters, "in_docker", False))
    child_image = normalize_docker_image(
        docker_image_for_node(node_name, getattr(parameters, "resource", None))
    )
    try:
        current_name = context.current_node_name
        current_resource = context.config.resource
    except RuntimeError:
        current_name = None
        current_resource = None
    current_image = normalize_docker_image(
        docker_image_for_node(current_name, current_resource)
    )

    if not in_container:
        if assignment_in_docker:
            decision = True
            reason = "child requires docker"
        else:
            decision = False
            reason = "not in docker"
    elif child_image is not None and current_image is not None and child_image == current_image:
        decision = False
        reason = "same image"
    else:
        decision = True
        if child_image is None:
            reason = "child image missing"
        elif current_image is None or child_image != current_image:
            reason = "images differ"
        else:
            reason = "wait for host"

    logger.info(
        "Nested docker dispatch: in_docker=%s assignment_in_docker=%s "
        "current node %s image %s child %s image %s decision=%s (%s)",
        in_container,
        assignment_in_docker,
        current_name,
        current_image,
        node_name,
        child_image,
        decision,
        reason,
    )
    return decision


def default_name_generator() -> str:
    return str("-".join(coolname.generate(2)))


def hashable_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return hashable_inputs(value)
    if isinstance(value, list):
        return [hashable_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(hashable_value(item) for item in value)
    if isinstance(value, dict):
        return {key: hashable_value(item) for key, item in value.items()}
    return value


def hashable_inputs(arg: Any) -> dict[str, Any]:
    """
    Get the hashable inputs for the node. This allows exclusion of some fields from the hash.

    Returns:
        dict: The hashable inputs.
    """
    return {
        key: hashable_value(value)
        for key, value in arg.__dict__.items()
        if key not in ["id"]
    }


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


def _is_self_resource(parameters: Optional[Parameters]) -> bool:
    if parameters is None:
        return False
    resource = getattr(parameters, "resource", None)
    return resource == "self"


def _slurm_parameters_are_unset(slurm: Optional[SlurmParameters]) -> bool:
    """True when slurm_parameters were omitted (or are an empty default)."""
    if slurm is None:
        return True
    fields_set = getattr(slurm, "model_fields_set", None)
    if not fields_set:
        return True
    for name in fields_set:
        value = getattr(slurm, name, None)
        if value is None or value == [] or value == {}:
            continue
        return False
    return True


def inherit_parent_slurm_parameters_for_self(
    parameters: Optional[Parameters],
    parent_parameters: Optional[Parameters],
) -> Optional[Parameters]:
    """Give a self-resource child the parent's slurm_parameters when it has none."""
    if parameters is None or not isinstance(parent_parameters, Parameters):
        return parameters
    if not _is_self_resource(parameters):
        return parameters
    if not _slurm_parameters_are_unset(getattr(parameters, "slurm_parameters", None)):
        return parameters

    parent_slurm = getattr(parent_parameters, "slurm_parameters", None)
    if not isinstance(parent_slurm, SlurmParameters):
        return parameters
    if _slurm_parameters_are_unset(parent_slurm):
        return parameters

    parameters.slurm_parameters = parent_slurm.model_copy(deep=True)
    logger.info(
        "Inherited parent slurm_parameters onto self-resource node: "
        "cpus_per_task=%s tasks=%s tasks_per_node=%s mem=%s mem_per_cpu=%s",
        getattr(parameters.slurm_parameters, "cpus_per_task", None),
        getattr(parameters.slurm_parameters, "tasks", None),
        getattr(parameters.slurm_parameters, "tasks_per_node", None),
        getattr(parameters.slurm_parameters, "mem", None),
        getattr(parameters.slurm_parameters, "mem_per_cpu", None),
    )
    return parameters


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

    def _parent_parameters_from_kwargs(self) -> Optional[Parameters]:
        parent_parameters = self._function_kwargs.get("parent_parameters", None)
        return parent_parameters if isinstance(parent_parameters, Parameters) else None

    def _apply_parent_slurm_for_self_resource(self) -> bool:
        """Fill empty slurm_parameters on resource self from the calling parent.

        Resource-assignment rules do not carry Slurm values for self, so this
        must happen on the node before ``execute_node_locally`` forwards
        ``self.parameters`` as ``parent_parameters``.
        """
        before = getattr(self.parameters, "slurm_parameters", None)
        inherit_parent_slurm_parameters_for_self(
            self.parameters, self._parent_parameters_from_kwargs()
        )
        after = getattr(self.parameters, "slurm_parameters", None)
        if after is before:
            return False
        if self.registry_entry is not None:
            self.registry_entry.parameters = self.parameters
        return True

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
        # function_mapping = await context.db.find_one(NodeModel, NodeModel.name == self.name)
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
            # input_table_name = await context.db.find_one(ModelMapping, ModelMapping.name == arg.__class__.__name__)
            input_table_name = context.model_mappings.get_by_name(arg.__class__.__name__)
            if input_table_name is None:
                logger.error(f"Could not find table name for {arg.__class__.__name__}")
                raise ValueError(f"Could not find table name for {arg.__class__.__name__}")
            if not isinstance(arg, Model):
                logger.error(f"{arg.__class__.__name__} is not an odmantic Model")
                raise ValueError(f"{arg.__class__.__name__} is not an odmantic Model")

            argument_entry = await context.db.save(arg)

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
            projects = await context.db.find(Project)
            if projects is None or len(projects) == 0:
                project = Project(field_name="default")
                await context.db.save(project)
                project_id = project.id
                delayed_message = f"default project: {project_id} "
            else:
                project = projects[0]
                project_id = project.id
                delayed_message = f"project: {project_id} "
        else:
            parent_registry_entry = await context.db.load_task_by_id(self.parent_id)
            project_id = parent_registry_entry.project
            delayed_message = f"using parent project: {project_id} "

        self.registry_entry = NodeRegistry(
            name=self.name,
            input_references=input_references,
            is_async=self.is_async,
            status=TaskStatus.RETRIEVED,
            custom_name=self.custom_name,
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
        parent_parameters = self._parent_parameters_from_kwargs()
        registry_entry = self.registry_entry
        assert registry_entry is not None
        await apply_resource_assignment_to_node_registry(
            context.db,
            registry_entry,
            parent_parameters=parent_parameters,
        )
        self.parameters = registry_entry.parameters
        self._apply_parent_slurm_for_self_resource()
        registry_entry.parameters = self.parameters
        await context.db.save(registry_entry)
        logger.info(
            f"Task task_id: {self.id} with name {self.name} created for resource: {registry_entry.parameters.resource} queue: {registry_entry.parameters.queue} with id: {self.id} and status: {registry_entry.status}"
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
        if context.db is None:
            raise ValueError("Database is not connected")

        arg_hash = compute_arg_hash(self._args)
        function_hash = cast(str, complex_hash_function(self._func))
        self._arg_hash = arg_hash
        self._function_hash = function_hash

        self.registry_entry = (
            await context.db.load_task(self.name, arg_hash, function_hash)
            if not self.parameters.force_rerun
            else None
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
                self.registry_entry.parent_ids.append(self.parent_id)
                await context.db.save(self.registry_entry)
            # whenever a task is found in the database, we may have to redo all child artifacts because the children
            # will not be loaded
            if self.recompute_artifacts:
                logger.debug(
                    f"Task task_id: {self.id} recomputing artifacts for task: {self.name}"
                )
                from simstack.core.recompute_artifacts import recompute_artifacts

                await recompute_artifacts(self.registry_entry)
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
        db = context.db
        assert self.registry_entry is not None
        logger.info(
            f"Task task_id: {self.id} loading results with task status {self.status}"
        )
        try:
            if self.registry_entry.status != TaskStatus.COMPLETED:
                return None
            
            simstack_result = SimstackResult(status=self.registry_entry.status)
            result = None
            for ref in self.registry_entry.results_references:
                model = await import_class(ref.variable_mapping, db)
                result = await db.find_one(model, model.id == ref.reference)
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

            if len(self.registry_entry.results_references) == 1:
                return result  # there is only one result, return it directly
            else:
                return simstack_result  # return the SimstackResult with all results

        except Exception as e:
            await self.set_status(TaskStatus.FAILED)
            logger.exception(f"Task task_id: {self.id} failed to load outputs: {e}")
            raise ValueError(f"Task task_id: {self.id} failed to load outputs: {e}")

    async def run_node_as_process(self) -> Union[Model, SimstackResult, None]:
        """Spawn a subprocess that runs ``run_node`` for the current id and resource, then load results."""
        import sys

        assert self.registry_entry is not None

        node_id = str(self.id)
        resource = str(self.parameters.resource)
        project_root = str(context.config.project_root)

        cmd = [
            sys.executable, "-m", "simstack.core.run_node",
            "--node-id", node_id,
            "--resource", resource,
            "--project-root", project_root,
        ]

        if context.in_docker:
            cmd.append("--in_docker")

        logger.info(
            "Task task_id: %s NEW spawning run_node subprocess: %s",
            self.id, " ".join(cmd),
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        # if stdout:
        #     logger.info("Task task_id: %s run_node stdout: %s", self.id, stdout.decode(errors="replace"))
        # if stderr:
        #     logger.warning("Task task_id: %s run_node stderr: %s", self.id, stderr.decode(errors="replace"))

        if proc.returncode != 0:
            logger.error(
                "Task task_id: %s run_node process exited with code %s",
                self.id, proc.returncode,
            )

        # Reload the registry entry from the database to pick up status changes made by the subprocess
        updated_entry = await context.db.load_task_by_id(self.id)
        if updated_entry is None:
            raise RuntimeError(
                f"Task task_id: {self.id} could not be found in the database after run_node subprocess"
            )
        self.registry_entry = updated_entry

        if self.registry_entry.status == TaskStatus.COMPLETED:
            return await self.load_results()
        return None

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
        if self.parameters.resource == resource_self:
            return await self.run_node_as_process()

        same_resource_default_queue = (
            context.config.resource == self.parameters.resource
            and self.parameters.queue == Queue.DEFAULT
        )
        if same_resource_default_queue:
            if should_dispatch_nested_docker(self.parameters, self.name):
                await self._persist_nested_docker_wait()
                logger.info(
                    "Task task_id: %s nested docker image differs; waiting for host runner",
                    self.id,
                )
                return await self._wait_for_remote_completion()
            return await self.run_node_as_process()

        if await self._submit_same_resource_slurm_node():
            logger.info(
                "Task task_id: %s submitted Slurm node directly from resource %s",
                self.id,
                context.config.resource,
            )
        return await self._wait_for_remote_completion()

    async def _persist_nested_docker_wait(self) -> None:
        """Mark the nested child for host ``run_docker`` without claiming it.

        Status stays SUBMITTED so the host runner can pick the task up.
        ``in_docker=True`` is required because NodeExecutionService only
        calls ``run_docker`` when that flag is set.
        """
        self.parameters.in_docker = True
        if self.registry_entry is None:
            return
        self.registry_entry.parameters = self.parameters
        await context.db.save(self.registry_entry)

    async def _wait_for_remote_completion(self) -> Union[Model, SimstackResult, None]:
        logger.info(f"entering wait for completion for task_id: {self.id}")
        await self.set_status(TaskStatus.SUBMITTED)
        while True:
            new_registry_entry = await context.db.load_task_by_id(self.id)
            # TODO add timeout mechanism here
            if new_registry_entry is None:
                raise RuntimeError(
                    f"Task task_id: {self.id} could not be found in the database"
                )
            new_status = new_registry_entry.status
            if (
                new_status != TaskStatus.RUNNING
                and new_status != TaskStatus.SUBMITTED
                and new_status != TaskStatus.SLURM_QUEUED
                and new_status != TaskStatus.RETRIEVED
            ):
                break

            print(f"Task task_id: {self.id} is waiting for results")
            await asyncio.sleep(5)

        if new_status == TaskStatus.COMPLETED:
            logger.info(f"Task task_id: {self.id} completed remotely")
            self.registry_entry = new_registry_entry
            return await self.load_results()
        return None

    async def _submit_same_resource_slurm_node(self) -> bool:
        """Submit a same-resource Slurm node before polling it.

        This is primarily needed when a parent already running on a resource
        creates a Slurm child for that resource. The same path also supports a
        top-level Python caller on the resource. The atomic claim prevents a
        concurrent resource runner from submitting the node twice.
        """
        if self.registry_entry is None:
            return False
        if self.parameters.queue != Queue.SLURM_QUEUE:
            return False
        if self.parameters.resource != context.config.resource:
            return False
        if not await claim_submitted_node(self.registry_entry):
            return False

        from simstack.core.submit_node import submit_node

        return await submit_node(self.registry_entry)

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
        self._apply_parent_slurm_for_self_resource()
        self.registry_entry.started_at = datetime.now()
        await self.set_status(TaskStatus.RUNNING)
        logger.info(
            f"Task task_id: {self.id} is started on {self.parameters.resource} in Node:execute_node_locally"
        )
        previous_node_name = context.current_node_name
        context.current_node_name = self.name
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
                # the child nodes. Resource self copies parent slurm first.
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
                    await context.db.save(self.registry_entry)
                logger.error(
                    f"Task task_id: {self.id} node function error for node: {self.name} msg: {str(e)}",
                    exc_info=True
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
            if new_task_status != TaskStatus.COMPLETED:
                return None
            return result
        except Exception:
            await self.set_status(TaskStatus.FAILED)
            raise
        finally:
            context.current_node_name = previous_node_name
            os.chdir(original_dir)
            logger.debug(
                f"Task task_id: {self.id} successfully back to directory: {original_dir.absolute()}"
            )

    async def process_results(self, result: Any) -> tuple[TaskStatus, Any]:
        assert self.registry_entry is not None
        # each of the following if sets the result either to a valid value or None
        new_task_status = TaskStatus.COMPLETED
        if result is None:
            logger.warning(f"Task task_id: {self.id} returned None")
            new_task_status = TaskStatus.FAILED  # result is None
        elif isinstance(result, bool):
            if not result:
                new_task_status = TaskStatus.FAILED
                result = None
        elif is_simstack_model(result) or isinstance(result, SimstackResult):
            if isinstance(result, SimstackResult):
                new_task_status = result.status
                if hasattr(result, "custom_name"):
                    self.registry_entry.custom_name = result.custom_name

                for file_stack in result.info_files:
                    if file_stack:
                        if isinstance(file_stack, FileStack):
                            logger.info(
                                f"Task task_id: {self.id} saving info file: {file_stack.name} {file_stack.id}"
                            )
                            if self.registry_entry.info_files is None:
                                self.registry_entry.info_files = FileList()
                            await context.db.save(file_stack)
                            self.registry_entry.info_files.append(file_stack)
                        else:
                            logger.error(
                                f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                            )
                    else:
                        logger.error(f"Task task_id: {self.id} saving info-file is NONE")
                        raise ValueError("saving info file is NONE")

                if result.error_message is not None and result.error_message != "":
                    logger.error(
                        f"Task task_id: {self.id} returned with error: {result.error_message}"
                    )
                if result.message is not None and result.message != "":
                    logger.info(f"Task task_id: {self.id} message: {result.message}")
            else:
                if hasattr(result, "status"):
                    new_task_status = result.status
                elif hasattr(result, "task_status"):
                    new_task_status = result.task_status

            results_references, result_models = await process_result_helper(result, str(self.id))
            self.registry_entry.results_references = results_references
            self.registry_entry.status = new_task_status

            if len(results_references) == 1:
                result = result_models[0]  # for a SimstackResult with just one returned model we return the model directly
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
        await context.db.save(self.registry_entry)
        logger.info(f"Task task_id: {self.id} {self.name} is set to {status}, id is: {self.id}")


async def _hydrate_embedded_file_stacks(
    value: Any,
    db: Any,
    resolved: dict[ObjectId, FileStack],
    seen: set[int] | None = None,
) -> Any:
    """
    Compatibility workaround, not the intended persistence pattern.

    FileStack objects must be stored as references. Embedding them directly
    inside other models violates this storage contract. This hydration logic
    compensates for existing models and records that violate the rule, but new
    models must not rely on it. The affected models and persisted data should
    eventually be migrated to proper references, after which this workaround
    should be removed.
    """
    if isinstance(value, FileStack):
        if value.content is not None or value.locations:
            return value

        file_stack_id = value.id
        if file_stack_id in resolved:
            return resolved[file_stack_id]

        canonical = await db.find_one(FileStack, FileStack.id == file_stack_id)
        if canonical is None:
            raise ValueError(f"Referenced FileStack {file_stack_id} not found")
        if canonical.id != file_stack_id:
            raise ValueError(
                f"Referenced FileStack {file_stack_id} resolved to {canonical.id}"
            )
        if canonical.content is None and not canonical.locations:
            raise ValueError(
                f"Referenced FileStack {file_stack_id} has no content or locations"
            )

        resolved[file_stack_id] = canonical
        return canonical

    if isinstance(value, tuple):
        hydrated_items = [
            await _hydrate_embedded_file_stacks(item, db, resolved, seen)
            for item in value
        ]
        if all(item is hydrated for item, hydrated in zip(value, hydrated_items)):
            return value
        return tuple(hydrated_items)

    if not isinstance(value, (BaseModel, list, dict)):
        return value

    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return value
    seen.add(value_id)

    try:
        if isinstance(value, BaseModel):
            for field_name in type(value).model_fields:
                field_value = getattr(value, field_name)
                hydrated = await _hydrate_embedded_file_stacks(
                    field_value, db, resolved, seen
                )
                if hydrated is not field_value:
                    setattr(value, field_name, hydrated)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = await _hydrate_embedded_file_stacks(
                    item, db, resolved, seen
                )
        else:
            for key, item in value.items():
                value[key] = await _hydrate_embedded_file_stacks(
                    item, db, resolved, seen
                )
        return value
    finally:
        seen.remove(value_id)


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
    db = context.db
    resolved_file_stacks: dict[ObjectId, FileStack] = {}

    for ref in registry_entry.input_references:
        try:
            model = await import_class(ref.variable_mapping, db)
            arg = await db.find_one(model, model.id == ref.reference)
            arg = await _hydrate_embedded_file_stacks(arg, db, resolved_file_stacks)
            args.append(arg)
        except Exception as e:
            logger.exception(
                f"Task task_id: {registry_entry.id} failed to load input {ref.variable_mapping} with id {ref.reference}: {str(e)}"
            )
            return None

    if registry_entry.arg_hash == "NOT INITIALIZED":
        logger.debug(f"Task task_id: {registry_entry.id} computes arg hashes")
        registry_entry.arg_hash = compute_arg_hash(args)


    logger.debug(
        f"Task task_id: {registry_entry.id} {registry_entry.name} loaded {len(args)} inputs in Node:node_from_database status: {registry_entry.status}"
    )
    func = None
    try:
        wrapped_func = await import_function(
            registry_entry.func_mapping, db, task_id=registry_entry.id
        )
        if wrapped_func is not None:
            # for nodes the mapping points to the wrapped func to we use that
            func = (
                wrapped_func if not hasattr(wrapped_func, "_inner") else wrapped_func._inner
            )
            logger.debug(
                f"Task task_id: {registry_entry.id} inner: {hasattr(wrapped_func, '_inner')} imported function: {func.__name__}"
            )
            if registry_entry.function_hash == "NOT INITIALIZED":
                registry_entry.function_hash = cast(str, complex_hash_function(func))
                registry_entry.is_async = asyncio.iscoroutinefunction(func)
        else:
            logger.error(
                f"Task task_id: {registry_entry.id} could not import function {registry_entry.func_mapping}"
            )
    except Exception as e:
        logger.error(
            f"Task task_id: {registry_entry.id} failed to import function {registry_entry.func_mapping} {str(e)}"
        )

    if func is None and registry_entry.function_hash == "NOT INITIALIZED":
        return None

    try:
        duplicate_entry = await db.find_one(
            NodeRegistry,
            (NodeRegistry.name == registry_entry.name)
            & (NodeRegistry.arg_hash == registry_entry.arg_hash)
            & (NodeRegistry.function_hash == registry_entry.function_hash)
            & (NodeRegistry.id != registry_entry.id),
        )
        if duplicate_entry is None or registry_entry.parameters.force_rerun:
            await db.save(registry_entry) # save the fixed entry AFTER checking for duplicates
            # the calling function may have the original entry unsaved!
        else:
            logger.info(f"Task task_id: {registry_entry.id} NEW DUPLICATE TREATMENT")
            logger.info(f"Task task_id: {registry_entry.id} found duplicate entry {duplicate_entry.id} {duplicate_entry.name}")
            # the parameters of the new job may be different

            registry_entry.populate_results_from_duplicate(duplicate_entry)
            if registry_entry.id not in duplicate_entry.parent_ids:
                duplicate_entry.parent_ids.append(registry_entry.id)
            await db.save(duplicate_entry)  # duplicate becomes a child of the new entry
            await db.save(registry_entry)

    except Exception as e:
        logger.exception(
            f"Task task_id: {registry_entry.id} failed during duplicate detection or secondary import {str(e)}"
        )
        return None

    if func is None:
        return None

    kwargs = {
        "func": func,
        "is_async": False,
        "call_path": registry_entry.call_path,
        "parameters": registry_entry.parameters,
        "custom_name": registry_entry.custom_name,
        "arg_hash": registry_entry.arg_hash,
        "function_hash": registry_entry.function_hash,
    }
    if hasattr(registry_entry, "is_async"):
        kwargs["is_async"] = registry_entry.is_async

    kwargs["parent_id"] = (
        registry_entry.parent_ids[0] if registry_entry.parent_ids else None
    )
    logger.debug(
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
    **kwargs_node: Any,
) -> Callable[[Callable[P, T]], Callable[..., T]]:
    ...


def node(
    _func: Optional[Callable[P, T]] = None,
    *,
    version: Optional[str] = None,
    **kwargs_node: Any,
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

        setattr(func, "_is_node", True)
        setattr(func, "_inner", func)
        setattr(func, "_node_parameters", _parameters_from_node_kwargs(kwargs_node))

        def update_kwargs(kwargs: dict[str, Any]) -> None:
            kwargs["func"] = func
            kwargs["is_async"] = is_async
            explicit_parameters = kwargs.pop("parameters", None)
            if explicit_parameters is None:
                kwargs["parameters"] = getattr(func, "_node_parameters").model_copy(
                    deep=True
                )
            elif isinstance(explicit_parameters, Parameters):
                kwargs["parameters"] = explicit_parameters.model_copy(deep=True)
            else:
                kwargs["parameters"] = Parameters.model_validate(
                    explicit_parameters
                ).model_copy(deep=True)
            kwargs["custom_name"] = kwargs.pop(
                "custom_name", kwargs_node.get("custom_name", default_name_generator())
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
            # TODO why do we run somewhere when already running ?
            elif status in [
                TaskStatus.SUBMITTED,
                TaskStatus.RETRIEVED,
                TaskStatus.SLURM_QUEUED,
            ]:
                result = await execution_node.run_somewhere()
            else:
                logger.warning(
                    f"Task task_id: {execution_node.id} status: {status} was not executed"
                )

            if result is None or execution_node.status != TaskStatus.COMPLETED:
                if execution_node.registry_entry is None:
                    raise RuntimeError(
                        f"Task task_id: {execution_node.id} node: {execution_node.name} has no registry entry"
                    )
                current_registry_entry = await context.db.find_one(
                    NodeRegistry, NodeRegistry.id == execution_node.registry_entry.id
                )
                if current_registry_entry is None:
                    raise RuntimeError(
                        f"Task task_id: {execution_node.id} node: {execution_node.name} registry entry disappeared"
                    )

                if (
                    current_registry_entry.status == TaskStatus.FAILED
                    and current_registry_entry.error
                ):
                    raise RuntimeError(f"task_id: {current_registry_entry.id} node: {current_registry_entry.name} failed with {current_registry_entry.error}")

                raise RuntimeError(
                    f"Task task_id: {current_registry_entry.id} node: {current_registry_entry.name} terminated with status {current_registry_entry.status}"
                )
            return cast(T, result)

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
            ran_somewhere = False
            if status == TaskStatus.COMPLETED:
                result = loop.run_until_complete(execution_node.load_results())
            elif status in [
                TaskStatus.SUBMITTED,
                TaskStatus.RETRIEVED,
                TaskStatus.SLURM_QUEUED,
            ]:
                result = loop.run_until_complete(execution_node.run_somewhere())
                ran_somewhere = True
            # Keep the established sync-node contract for resultless/default-queue
            # executions. Slurm submission failures must not be mistaken for that
            # contract, because they otherwise disappear as a successful ``None``.
            if (
                ran_somewhere
                and result is None
                and execution_node.parameters.queue != Queue.SLURM_QUEUE
            ):
                return cast(T, result)
            if result is None or execution_node.status != TaskStatus.COMPLETED:
                if (
                    execution_node.registry_entry is not None
                    and execution_node.registry_entry.status == TaskStatus.FAILED
                    and execution_node.registry_entry.error
                ):
                    raise RuntimeError(
                        f"task_id: {execution_node.registry_entry.id} node: {execution_node.registry_entry.name} failed with {execution_node.registry_entry.error}")



                raise RuntimeError(
                    f"Task task_id: {execution_node.id} node: {execution_node.name} terminated with status {execution_node.status}"
                )
            return cast(T, result)

        setattr(async_wrapper, "is_node", True)
        setattr(sync_wrapper, "is_node", True)
        # Return the appropriate wrapper based on whether the function is async
        if is_async:
            return cast(Callable[..., T], async_wrapper)
        else:
            return cast(Callable[..., T], sync_wrapper)

    setattr(decorator, "is_node", True)

    if _func is None:
        # Called with parameters: @node(...)
        return decorator
    else:
        # Called without parameters: @node
        return decorator(_func)
