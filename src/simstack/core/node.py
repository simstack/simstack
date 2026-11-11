import asyncio
import functools
import inspect
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from typing import (
    Any,
    Callable,
    Optional,
    TypeVar,
    cast,
    List,
    ParamSpec,
    Union,
    overload,
)

import coolname  # type: ignore[import-untyped]
import nest_asyncio  # type: ignore[import-untyped]
from simstack.models.document import Model, ObjectId
from pydantic import BaseModel

from simstack.core.artifacts import create_artifacts, ArtifactArguments
from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.hash import complex_hash_function
from simstack.core.node_runner import NodeRunner
from simstack.core.process_results import process_result_helper
from simstack.core.run_node_protocol import RESULT_PREFIX, parse_run_node_result
from simstack.core.simstack_result import SimstackResult
from simstack.core.task_id import set_task_id, clear_task_id
from simstack.models import Parameters
from simstack.models import NodeRegistry, NamedDataReference
from simstack.models.file_list import FileList
from simstack.models.files import FileStack
from simstack.models.parameters import Queue, SlurmParameters
from simstack.models.simstack_model import is_simstack_model
from simstack.util.importer import import_function
from simstack.util.sanitized_output import sanitized_command, sanitized_tail

logger = logging.getLogger("Node")

# Concurrent calls in one scientific parent are separate consumers of its
# Server dependency. Keep an owned launch alive while any call still awaits it.
_waiter_counts: dict[tuple, int] = {}
_consumer_operations: set[asyncio.Task] = set()

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
    lookup_resource = str(config.resource) if task_resource == "self" else task_resource
    try:
        program_config = resource_config.get_program(node_name, resource=lookup_resource)
    except ValueError:
        return None
    image = program_config.get("docker_image") if program_config else None
    if not isinstance(image, str) or not image.strip():
        return None
    return image


def should_handoff_nested_execution(
    parameters: Parameters, node_name: str
) -> bool:
    """True when a child must be handed from the current process to the host.

    On the host, only Docker tasks need a handoff to the runner. In a
    container, an explicit non-Docker task always returns to the host. A
    Docker task stays inline only when both images are known and equal.
    ``Parameters.separate_cloud_vm`` hands the child off even when the image
    matches, so the cloud poller can start another VM.
    """
    if getattr(parameters, "separate_cloud_vm", False):
        logger.info(
            "Nested execution handoff: separate_cloud_vm child %s decision=True",
            node_name,
        )
        return True

    in_container = process_is_in_docker()
    assignment_in_docker = bool(getattr(parameters, "in_docker", False))
    child_image = None
    current_image = None
    current_name = None

    try:
        current_name = context.current_node_name
        current_resource = context.config.resource
    except RuntimeError:
        current_resource = None

    if not in_container:
        if assignment_in_docker:
            decision = True
            reason = "child requires docker"
        else:
            decision = False
            reason = "not in docker"
    elif not assignment_in_docker:
        decision = True
        reason = "child explicitly requires host execution"
    else:
        child_image = normalize_docker_image(
            docker_image_for_node(node_name, getattr(parameters, "resource", None))
        )
        current_image = normalize_docker_image(
            docker_image_for_node(current_name, current_resource)
        )
        if (
            child_image is not None
            and current_image is not None
            and child_image == current_image
        ):
            decision = False
            reason = "same image"
        else:
            decision = True
            if child_image is None:
                reason = "child image missing"
            else:
                reason = "images differ"

    logger.info(
        "Nested execution handoff: in_docker=%s assignment_in_docker=%s "
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


class _HashableModel(dict[str, Any]):
    """A typed model projection, distinct from an ordinary scientific mapping."""


def hashable_value(value: Any) -> Any:
    if isinstance(value, FileStack):
        return value
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
    primary = type(arg).__primary_field__ if isinstance(arg, Model) else None
    values = {
        key: hashable_value(getattr(arg, key))
        for key in type(arg).model_fields
        if key != primary
    }
    # Scientific graphs carry materialized collection rows as members. They
    # affect execution even when their declared fields contain only record IDs.
    if "_cache" in vars(arg):
        values["_cache"] = hashable_value(vars(arg)["_cache"])
    return _HashableModel(
        model_type=f"{type(arg).__module__}.{type(arg).__qualname__}",
        fields=values,
        extras=hashable_value(arg.model_extra or {}),
    )


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


def _explicit_cloud_child_needs_own_vm(parameters: Optional[Parameters]) -> bool:
    """True when a cloud assignment from a cloud process needs another VM.

    ``self`` stays on the VM that is already running. An explicit ``cloud``
    resource with the default queue stays submitted so the Hetzner
    orchestrator provisions a VM from that child's slurm parameters, including
    when the Docker image matches the caller.
    """
    if parameters is None or _is_self_resource(parameters):
        return False
    if parameters.resource != "cloud" or parameters.queue != Queue.DEFAULT:
        return False
    try:
        current_resource = context.config.resource
    except RuntimeError:
        return False
    return current_resource == "cloud"


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
    execution environment, and interactions with the Server API. This class helps in
    defining tasks, storing their details, and ensuring they are executed either
    locally or remotely, with their statuses tracked by Server
    context.

    :ivar name: The name of the function associated with the node.
    :type name: str
    :ivar custom_name: A custom name for the node, generated if not specified.
    :type custom_name: str or None
    :ivar registry_entry: The Server task snapshot associated with this node.
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
        self._parent_execution = None
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
        self._execution_return_kind: str | None = None
        self._owns_inline_nested_execution = False

    def _parent_parameters_from_kwargs(self) -> Optional[Parameters]:
        function_kwargs = getattr(self, "_function_kwargs", {})
        parent_parameters = function_kwargs.get("parent_parameters", None)
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
        """Publish an invocation; Server resolves cache, lineage and routing."""
        from simstack.runner_runtime.launch_claims import (
            current_execution, finish, receipt, reserve_launch,
        )

        mapping = context.node_mappings.get_by_name(self.name)
        if mapping is None:
            raise ValueError(f"Could not find function mapping for name: {self.name}")
        for arg in self._args:
            if not isinstance(arg, Model):
                raise ValueError(f"{type(arg).__name__} is not a SimStack Model")
        self._apply_parent_slurm_for_self_resource()
        self.parameters.recompute_artifacts = bool(self.recompute_artifacts)
        invocation = {
            "parent_execution": current_execution() if self.parent_id else None,
            "name": self.name,
            "function_mapping": mapping.function_mapping,
            "version": getattr(self._func, "_node_version", None),
            "repository_revision": None if self.parent_id else getattr(mapping, "repository_revision", None),
            "is_async": self.is_async,
            "arg_hash": arg_hash,
            "call_path": self.call_path,
            "parameters": self.parameters.model_dump(mode="json", exclude_unset=True),
            "custom_name": self.custom_name,
            "recompute": self.recompute_artifacts,
        }
        if self.parent_id and not invocation["parent_execution"]:
            raise RuntimeError("A child invocation requires its parent's execution identity")
        self._parent_execution = dict(invocation["parent_execution"]) if self.parent_id else None
        self._dependency_server = context.server if self.parent_id else None
        resolved = await context.server.tasks.resolve_invocation(invocation, request_id=str(uuid4()))
        from simstack.server_api.scientific_values import contains_file_values
        prepared = None
        cached = None
        if contains_file_values(self._args):
            values, prepared = await context.server.values.preview_task_inputs(
                self._args, repository_revision=resolved["repository_revision"])
            invocation["arg_hash"] = compute_arg_hash(values)
            cached = await context.server.tasks.reuse_invocation(invocation, request_id=str(uuid4()))
        if cached is None or cached["task"] is None:
            publication_options = {"prepared": prepared} if prepared is not None else {}
            values, input_publication = await context.server.values.publish_task_inputs(
                self._args, repository_revision=resolved["repository_revision"], **publication_options)
            # Recheck the accepted snapshot after a miss: object metadata may
            # have changed since preview. Admission always uses its final hash.
            invocation["arg_hash"] = compute_arg_hash(values)
            cached = await context.server.tasks.reuse_invocation(invocation, request_id=str(uuid4()))
        # Serializers and resolution hooks can normalize values. Cache lookup
        # and admission both use a resolved scientific snapshot's hash.
        self._args = values
        self._arg_hash = invocation["arg_hash"]
        if cached["task"] is not None:
            self.registry_entry = cached["task"]
            self.parameters = self.registry_entry.parameters
            self.recompute_artifacts = cached["recompute_artifacts"]
            self._owns_inline_nested_execution = False
            if self.registry_entry.status in {TaskStatus.RETRIEVED, TaskStatus.SUBMITTED}:
                await self._recover_inline_launch()
            elif getattr(self.registry_entry, "_execution_local", False):
                await self._recover_inline_launch(observe_only=True)
            if cached["disposition"] == "reused_complete" and self.recompute_artifacts:
                from simstack.core.recompute_artifacts import recompute_artifacts
                await recompute_artifacts(self.registry_entry)
            return self.registry_entry
        effective = Parameters.model_validate(cached["parameters"])
        logical_self = cached["logical_self"]
        # Physical container/VM compatibility belongs to the actual launcher.
        # Cache/rule resolution remains Server-owned, so evaluate the original
        # handoff guards against its effective route before requesting a claim.
        inline = bool(self.parent_id and logical_self)
        if not inline:
            same_resource = str(effective.resource) == str(context.config.resource)
            inline = bool((logical_self or (same_resource and effective.queue == Queue.DEFAULT))
                          and not (not logical_self and _explicit_cloud_child_needs_own_vm(effective))
                          and not should_handoff_nested_execution(effective, self.name))
        local_root = self.parent_id is None and context.server.requests.authorization == "user" and inline
        inline = inline and (self.parent_id is not None or local_root)
        names = list(inspect.signature(self._func).parameters)
        references = [NamedDataReference.from_variable(
            value, variable_name=names[i] if i < len(names) else f"arg_{i}",
        ).model_dump(mode="json") for i, value in enumerate(values)]
        requested_task_id = str(ObjectId())
        request_id = str(uuid4())
        payload = {
            **invocation,
            "requested_task_id": requested_task_id,
            "invocation_id": str(uuid4()),
            "input_references": references,
            "input_publication": input_publication,
            "inline": inline,
            "expected_route": cached["route"] if inline else None,
        }
        reservation = None
        intent = None
        if self.parent_id or local_root:
            reservation = reserve_launch(context.server, requested_task_id)
            if reservation is None:
                raise RuntimeError(f"Task {requested_task_id} already has a launch reservation")
            intent = receipt(
                context.server, requested_task_id, begin=True,
                revision=payload["repository_revision"], inline=payload["inline"],
                publication=payload, request_id=request_id,
            )
        try:
            response = await (
                context.server.tasks.create_child(payload, request_id=request_id)
                if self.parent_id else context.server.tasks.submit_root(payload, request_id=request_id)
            )
            self.registry_entry = response["task"]
            if response.get("local_execution_token"):
                from simstack.server_api import ServerAPI
                self._execution_server = ServerAPI.for_local_execution(
                    context.server.requests.server_url, response["local_execution_token"],
                    resource=self.registry_entry._execution_resource)
                self._execution_server.requests.bootstrap_data = context.server.requests.bootstrap_data
            self.parameters = self.registry_entry.parameters
            self.recompute_artifacts = response["recompute_artifacts"]
            disposition = response["disposition"]
            self._owns_inline_nested_execution = disposition == "launch_inline"
            if self._owns_inline_nested_execution:
                if reservation is None:
                    raise RuntimeError("An inline launch requires its publisher reservation")
                if str(self.registry_entry.id) != requested_task_id:
                    raise RuntimeError("Server returned a different task for this inline publication")
                # Atomically replace the pending receipt with its canonical
                # claim. Never unlink it between publication and launch.
                intent = receipt(context.server, self.registry_entry.id, begin=True,
                    revision=self.registry_entry.repository_revision,
                    generation=getattr(self.registry_entry, "_execution_generation", None),
                    inline=True, claim_id=getattr(self.registry_entry, "_execution_claim_id"),
                    publication=payload, request_id=request_id)
                finish(context.server, intent, claimed=True)
                self._inline_launch_reservation = reservation
                object.__setattr__(self.registry_entry, "_launch_receipt", intent)
                reservation = None
            elif intent is not None:
                finish(context.server, intent, claimed=False)
            if disposition == "reused_complete" and self.recompute_artifacts:
                from simstack.core.recompute_artifacts import recompute_artifacts
                await recompute_artifacts(self.registry_entry)
            return self.registry_entry
        finally:
            if reservation is not None:
                reservation.close()

    async def get_node_registry(self) -> TaskStatus:
        self._arg_hash = compute_arg_hash(self._args)
        self._function_hash = ""
        await self.make_registry_entry("", self._arg_hash)
        return self.registry_entry.status

    async def load_results(
        self, return_kind: str | None = None
    ) -> Union[Model, SimstackResult, bool, None]:
        """
        Loads the results associated with a specific task through the Server API. This
        method verifies whether the task has valid result identifiers. If valid
        identifiers (results_id and results_table_name) exist, it attempts to fetch
        the outputs.

        If the task status is not TaskStatus.COMPLETED None is returned which results in a RuntimeError
        If the results are not found or if the retrieval process fails, a `ValueError` is raised.

        :raises ValueError: If the task has completed but lacks output identifiers
            (`results_id` or `results_table_name`), or if there is any error
            during the process of loading the results.

        :return: The retrieved scientific task outputs.
        """
        assert self.registry_entry is not None
        logger.info(
            f"Task task_id: {self.id} loading results with task status {self.status}"
        )
        try:
            if self.registry_entry.status != TaskStatus.COMPLETED:
                return None
            persisted_return_kind = return_kind or getattr(
                self.registry_entry, "return_kind", None
            )
            if persisted_return_kind == "none":
                return None
            if persisted_return_kind == "bool":
                return True
            if not self.registry_entry.results_references:
                if persisted_return_kind == "multiple":
                    return SimstackResult(
                        status=self.registry_entry.status,
                        custom_name=self.registry_entry.custom_name,
                        message=self.registry_entry.message,
                        error_message=self.registry_entry.error,
                        info_files=list(self.registry_entry.info_files),
                    )
                return True
            
            simstack_result = SimstackResult(
                status=self.registry_entry.status,
                custom_name=self.registry_entry.custom_name,
                message=self.registry_entry.message,
                error_message=self.registry_entry.error,
                info_files=list(self.registry_entry.info_files),
            )
            result = None
            values = await context.server.values.resolve_task_outputs(self.registry_entry)
            if len(values) != len(self.registry_entry.results_references):
                raise ValueError("Task output snapshot does not match its result references")
            for ref, result in zip(self.registry_entry.results_references, values):
                if result is None:
                    logger.error(
                        f"Task task_id: {self.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                    )
                    raise ValueError(
                        f"Task task_id: {self.id} could not find result with id {ref.reference} in table {ref.variable_mapping}"
                    )
                simstack_result.__setattr__(ref.variable_name, result)

            logger.info(f"Task task_id: {self.id} loaded outputs")

            if (
                len(self.registry_entry.results_references) == 1
                and persisted_return_kind != "multiple"
            ):
                return result  # there is only one result, return it directly
            else:
                return simstack_result  # return the SimstackResult with all results

        except Exception as exc:
            # A read failure does not invalidate a completed scientific task.
            raise ValueError(f"Task {self.id} failed to load outputs: {exc}") from exc

    async def _record_process_failure(
        self, launch_entry: NodeRegistry, error: str, *, returncode: int | None = None,
        pending=None,
    ) -> None:
        """Fallback is fenced by the original launch, never by a freshly read one."""
        termination = {
            "exit_code": returncode if returncode is not None and returncode >= 0 else None,
            "signal": -returncode if returncode is not None and returncode < 0 else None,
        }
        try:
            updated = await context.server.tasks.fail_attempt(
                launch_entry, error, phase="subprocess", **termination,
            )
            if updated is not None and self.registry_entry is not None:
                self.registry_entry = updated
        except Exception as exc:
            logger.warning("Could not record subprocess failure for task %s", launch_entry.id, exc_info=True)
            if pending is not None:
                return  # The original physical receipt is retained by the caller.
            from simstack.server_api.http_transport import RunnerAPIError
            if isinstance(exc, (RunnerAPIError, OSError)):
                from simstack.runner_runtime.pending_execution_failures import record_failure
                try:
                    record_failure(error, phase="subprocess", execution=context.server.tasks.execution(launch_entry),
                        server=context.server, resource=getattr(launch_entry, "_execution_resource", None),
                        rejected_status=getattr(exc, "status_code", None), **termination)
                except OSError:
                    logger.warning("Could not journal subprocess failure for task %s", launch_entry.id, exc_info=True)

    async def _confirm_owned_process_exit(self, launch_entry, process, *, error=None):
        """A surviving launcher can ACK the exact controller it has drained."""
        if (process is None or process.returncode is None
                or not getattr(launch_entry, "_execution_local", False)):
            return
        from simstack.core.services.task_termination import stop_task_execution
        from simstack.server_api.http_transport import RunnerAPIError
        while True:
            try:
                current = await context.server.tasks.get_task(launch_entry.id)
                if (current is None or context.server.tasks.execution(current) != context.server.tasks.execution(launch_entry)
                        or (getattr(current, "_execution_owner", None) or {}).get("process_id") != process.pid):
                    return
                terminal = current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}
                if not terminal and (error is None or current.status == TaskStatus.TERMINATING):
                    return
                await stop_task_execution(current, server=context.server, runner_pid=os.getpid(),
                    workdir=context.config.workdir, local_process=process)
                if not terminal:
                    current = await context.server.tasks.fail_attempt(launch_entry, error, phase="subprocess",
                        exit_code=process.returncode if process.returncode >= 0 else None,
                        signal=-process.returncode if process.returncode < 0 else None)
                await context.server.tasks.termination_confirmed(
                    launch_entry, current.error or "Scientific execution stopped")
                observation = getattr(launch_entry, "_native_observation", None)
                if observation is not None:
                    from simstack.runner_runtime.native_observations import retire
                    try:
                        retire(context.server, observation)
                    except OSError:
                        logger.warning("Task %s retains an acknowledged native observation", launch_entry.id, exc_info=True)
                return
            except RunnerAPIError as exc:
                if exc.status_code in {401, 403, 404, 409}:
                    return  # A replacement execution never receives the old ACK.
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                logger.warning("Task %s awaits its owned-process stop acknowledgement", launch_entry.id, exc_info=True)
                await asyncio.sleep(1)

    async def run_node_as_process(
        self,
    ) -> Union[Model, SimstackResult, bool, None]:
        """Run this task in an isolated child and restore its persisted result."""
        from simstack.runner_runtime.execution_environment import task_environment, task_launch_command, task_project_root

        assert self.registry_entry is not None
        launch_entry = self.registry_entry
        from simstack.runner_runtime.launch_claims import task_execution, current_execution
        # Allocate once for this actual process; the environment and fallback
        # keep this identity even after a canonical task refresh.
        with task_execution(launch_entry):
            execution = current_execution()
            if execution is None:
                raise RuntimeError("A subprocess launch requires its claim identity")
            object.__setattr__(launch_entry, "_execution_start_id", execution["start_id"])

        resource = (
            str(context.config.resource)
            if self.parameters.resource == "self"
            else str(self.parameters.resource)
        )
        project_root = str(task_project_root(launch_entry, context.config.project_root))
        cmd = task_launch_command(launch_entry, resource, project_root, in_docker=context.in_docker,
            local_user=getattr(launch_entry, "_execution_local", False))

        clean_cmd = sanitized_command(cmd, None)
        python_path_entries = [project_root]
        if not context.in_docker:
            python_path_entries.extend(
                str(path)
                for path in (getattr(context.config, "python_paths", None) or [])
            )
        inherited_python_path = os.environ.get("PYTHONPATH")
        if inherited_python_path:
            python_path_entries.append(inherited_python_path)
        child_python_path = os.pathsep.join(
            dict.fromkeys(path for path in python_path_entries if path)
        )

        logger.info(
            "Task task_id: %s spawning run_node subprocess: %s",
            self.id,
            " ".join(clean_cmd),
        )

        proc = None
        try:
            # A shared child may outlive this launcher. Regular files do not
            # break its output when the parent process closes its read handles.
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                from simstack.runner_runtime.launch_claims import inherit_launch_reservation
                from simstack.runner_runtime.native_observations import observe_local_launch
                environment = {**task_environment(self.registry_entry, server=context.server), "PYTHONPATH": child_python_path}
                with (inherit_launch_reservation(getattr(self, "_inline_launch_reservation", None), environment) as ownership,
                      observe_local_launch(context.server, launch_entry, environment, ownership,
                          enabled=getattr(launch_entry, "_execution_local", False)
                              and not self.parameters.in_docker and not context.in_docker) as ownership):
                    from contextlib import nullcontext
                    job_context = nullcontext((None, ownership))
                    creationflags = 0
                    if os.name == "nt":
                        from simstack.runner_runtime.windows_science_job import controller_job, child_creationflags
                        creationflags = child_creationflags()
                        if getattr(launch_entry, "_execution_local", False) and not self.parameters.in_docker:
                            job_context = controller_job(context.server, launch_entry, environment, ownership)
                    with job_context as (science_job, child_ownership):
                        if (os.name == "nt" and science_job is None
                                and not self.parameters.in_docker):
                            from simstack.runner_runtime.windows_science_job import job_name, spawn_job_process
                            proc = spawn_job_process(cmd, name=job_name(context.server, launch_entry),
                                cwd=project_root, env=environment, stdout=stdout_file, stderr=stderr_file,
                                startupinfo=child_ownership.get("startupinfo"), retain_in_child=True,
                                creationflags=creationflags)
                        else:
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=stdout_file,
                                stderr=stderr_file,
                                cwd=project_root,
                                env=environment,
                                start_new_session=os.name != "nt",
                                creationflags=creationflags,
                                **child_ownership,
                            )
                        if science_job is not None:
                            import weakref
                            proc._simstack_windows_job = science_job
                            weakref.finalize(proc, science_job.close)
                if os.name != "nt" and isinstance(proc, asyncio.subprocess.Process):
                    proc._simstack_process_group = True
                from simstack.runner_runtime.launch_claims import record_spawn
                record_spawn(context.server, launch_entry, proc.pid)
                from simstack.core.services.task_termination import track_local_process
                with track_local_process(launch_entry, proc, server=context.server):
                    if (getattr(launch_entry, "_execution_local", False)
                            or (getattr(launch_entry, "_execution_launch_kind", None) == "inline"
                                and launch_entry.parameters.queue == "slurm-queue")):
                        from simstack.core.services.task_termination import monitor_task_process
                        stdout_b, stderr_b = await monitor_task_process(launch_entry, proc,
                            server=context.server, runner_pid=os.getpid(), workdir=context.config.workdir)
                    else:
                        stdout_b, stderr_b = await proc.communicate()
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout_b, stderr_b = stdout_file.read(), stderr_file.read()
        except asyncio.CancelledError:
            # TaskGroup cancellation is a BaseException, so the handler below
            # never runs. Kill this child or its row stays RUNNING.
            from simstack.core.services.fail_children import fail_abandoned_child

            pids: tuple[int, ...] = ()
            if proc is not None and proc.returncode is None:
                pid = getattr(proc, "pid", None)
                if type(pid) is int and pid > 0:
                    pids = (pid,)
                elif pid is not None:
                    logger.error(
                        "Task task_id: %s process_id must be a positive int, got %r",
                        self.id,
                        pid,
                    )
            from simstack.core.context import execution_server
            with execution_server(getattr(self, "_dependency_server", None)):
                await fail_abandoned_child(
                    self.registry_entry,
                    parent_id=self.parent_id,
                    parent_execution=self._parent_execution,
                    runner_pid=os.getpid(),
                    extra_pids=pids,
                    local_process=proc,
                )
            # stop_task_execution already waits for an owned process it stops.
            # A shared child keeps running for its other consumers.
            raise
        except Exception as exc:
            error = sanitized_tail(str(exc), None)
            if proc is not None and (proc.returncode is None or (getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None))):
                from simstack.core.services.task_termination import stop_task_execution
                await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                    workdir=context.config.workdir, local_process=proc)
            if proc is None or proc.returncode is not None:
                await self._record_process_failure(launch_entry, error, returncode=proc.returncode if proc is not None else None)
                await self._confirm_owned_process_exit(launch_entry, proc, error=error)
            if proc is None and isinstance(exc, OSError):
                # exec/CreateProcess failed without returning a child handle.
                observation = getattr(launch_entry, "_native_observation", None)
                if observation is not None:
                    from simstack.runner_runtime.native_observations import retire
                    try:
                        retire(context.server, observation)
                    except OSError:
                        logger.warning("Task %s retains its failed-spawn observation", launch_entry.id, exc_info=True)
            raise RuntimeError(
                f"Task task_id: {self.id} could not start run_node child: {error}"
            ) from None
        stdout = sanitized_tail(stdout_b, None)
        stderr = sanitized_tail(stderr_b, None)
        outcome = parse_run_node_result(stdout)
        pending = None
        if proc.returncode:
            from simstack.runner_runtime.pending_execution_failures import cleanup_scope, record_observed_failure
            cleanup = None
            if getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None):
                cleanup = {"container": self.parameters.in_docker, "scope": cleanup_scope(),
                    "process_group": proc.pid if getattr(proc, "_simstack_process_group", False) else None,
                    "windows_job": getattr(getattr(proc, "_simstack_windows_job", None), "name", None)}
            pending = record_observed_failure(
                (outcome.error if outcome is not None else None)
                or f"run_node child exited with code {proc.returncode}",
                phase="subprocess", execution=execution, server=context.server, resource=resource,
                return_kind=outcome.return_kind if outcome is not None else "exception",
                exit_code=proc.returncode if proc.returncode >= 0 else None,
                signal=-proc.returncode if proc.returncode < 0 else None,
                cleanup=cleanup,
            )
        try:
            from simstack.core.services.task_termination import stop_owned_process_group, stop_task_execution, TaskTerminationError
            from simstack.server_api.http_transport import RunnerAPIError
            while True:
                try:
                    if getattr(proc, "_simstack_windows_job", None) is not None:
                        await stop_owned_process_group(proc)
                    if proc.returncode != 0 and (getattr(proc, "_simstack_process_group", False) or getattr(proc, "_simstack_windows_job", None)):
                        await stop_task_execution(launch_entry, server=context.server, runner_pid=os.getpid(),
                            workdir=context.config.workdir, local_process=proc)
                    break
                except (OSError, TaskTerminationError, RunnerAPIError):
                    # Retain the receipt and physical ownership; journal replay
                    # cannot terminalize active science until cleanup is proved.
                    logger.warning("Task %s retains its inline worker until cleanup is verified", self.id, exc_info=True)
                    await asyncio.sleep(1)
            if pending is not None and "cleanup" in pending[1]:
                pending[1].pop("cleanup")
                if pending[0] is not None:
                    from simstack.runner_runtime.pending_execution_failures import record_failure
                    try:
                        record_failure(server=context.server, resource=resource, **pending[1])
                    except OSError:
                        # The guarded receipt remains recoverable; a live API
                        # can still retain the now-verified physical evidence.
                        logger.warning("Task %s retains its guarded exit receipt", self.id, exc_info=True)
                        pending = None, pending[1]

            # Read the original attempt outcome recorded by the subprocess.
            try:
                updated_entry = await context.server.tasks.get_outcome(launch_entry)
            except Exception as exc:
                if proc.returncode == 0 and outcome is not None and outcome.success:
                    # Success follows Server's completion acknowledgement. A bool
                    # needs no further data; model references still require a read.
                    from simstack.server_api.http_transport import RunnerAPIError
                    if (outcome.return_kind == "bool" and isinstance(exc, RunnerAPIError)
                            and (exc.status_code is None or exc.status_code >= 500)):
                        self.registry_entry.status = TaskStatus.COMPLETED
                        self.registry_entry.return_kind = "bool"
                        return True
                    raise
                logger.warning("Could not read child outcome for task %s", self.id, exc_info=True)
                updated_entry = launch_entry
            if updated_entry is None:
                raise RuntimeError(
                    f"Task task_id: {self.id} has no recorded outcome after run_node subprocess"
                )
            if (updated_entry.status == TaskStatus.TERMINATING and proc.returncode is not None
                    and (getattr(launch_entry, "_execution_local", False)
                         or getattr(launch_entry, "_execution_launch_kind", None) == "inline")):
                # A dead controller may leave a worker or Docker container alive.
                # Verify physical cleanup before acknowledging the original attempt.
                from simstack.core.services.task_termination import stop_task_execution
                await stop_task_execution(updated_entry, server=context.server, runner_pid=os.getpid(),
                    workdir=context.config.workdir, local_process=proc)
                updated_entry = await context.server.tasks.termination_confirmed(
                    launch_entry, updated_entry.error or "Terminated by user")
            if (getattr(updated_entry, "_execution_owner", None)
                    and updated_entry._execution_owner["process_id"] == proc.pid
                    and updated_entry.status != TaskStatus.TERMINATING):
                from simstack.core.services.task_termination import stop_task_execution
                await stop_task_execution(updated_entry, server=context.server, runner_pid=os.getpid(),
                    workdir=context.config.workdir, local_process=proc)
            await self._confirm_owned_process_exit(launch_entry, proc)
            self.registry_entry = updated_entry
            committed_kind = getattr(updated_entry, "return_kind", None)
            committed = (
                updated_entry.status == TaskStatus.COMPLETED
                and not updated_entry.error
                and committed_kind in {"bool", "model", "multiple", "none"}
            )
            if committed and (proc.returncode != 0 or outcome is None or not outcome.success):
                logger.warning("Task %s completed before child launcher exited with code %s", self.id, proc.returncode)

            if not committed and (proc.returncode != 0 or outcome is None or not outcome.success):
                child_error = self.registry_entry.error or (
                    outcome.error if outcome is not None else None
                )
                details = []
                visible_stdout = "\n".join(
                    line for line in stdout.splitlines() if not line.startswith(RESULT_PREFIX)
                ).strip()
                if visible_stdout:
                    details.append(f"stdout:\n{visible_stdout}")
                if stderr:
                    details.append(f"stderr:\n{stderr}")
                detail_tail = sanitized_tail(
                    "\n".join(detail for detail in details if detail),
                    None,
                    limit=3500,
                )
                error = sanitized_tail(child_error, None, limit=None) if child_error else f"run_node child exited with code {proc.returncode}"
                if self.registry_entry.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                        TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}:
                    await self._record_process_failure(launch_entry, error, returncode=proc.returncode, pending=pending)
                    await self._confirm_owned_process_exit(launch_entry, proc, error=error)
                if detail_tail:
                    logger.warning("Task %s child diagnostics: %s", self.id, detail_tail)
                raise RuntimeError(f"Task task_id: {self.id} {error}")

            if self.registry_entry.status != TaskStatus.COMPLETED:
                error = (
                    f"run_node child reported success but task status is "
                    f"{self.registry_entry.status}"
                )
                if self.registry_entry.status in {TaskStatus.SUBMITTED, TaskStatus.RETRIEVED, TaskStatus.RUNNING,
                        TaskStatus.SLURM_QUEUED, TaskStatus.SLURM_RUNNING}:
                    await self._record_process_failure(launch_entry, error, returncode=proc.returncode, pending=pending)
                    await self._confirm_owned_process_exit(launch_entry, proc, error=error)
                raise RuntimeError(f"Task task_id: {self.id} {error}")

            if stderr:
                logger.warning("Task task_id: %s run_node stderr: %s", self.id, stderr)
            return_kind = committed_kind if committed else outcome.return_kind
            if return_kind == "bool":
                return True
            if return_kind in {"model", "multiple"}:
                return await self.load_results(return_kind=return_kind)
            if return_kind == "none":
                return None
            error = (
                f"Task task_id: {self.id} returned unsupported child result kind "
                f"{return_kind!r}"
            )
            await self._record_process_failure(launch_entry, error, returncode=proc.returncode, pending=pending)
            raise RuntimeError(error)
        finally:
            if pending is not None:
                # Terminal science keeps its result/primary error; physical
                # evidence belongs to this exact launch, even after a rerun.
                from simstack.core.run_node import _retain_worker_exit
                await _retain_worker_exit(context.server, pending, resource)

    async def run_somewhere(self) -> Union[Model, SimstackResult, bool, None]:
        """Cancel a parent dependency only when its last local caller leaves."""
        parent = getattr(self, "_parent_execution", None)
        if not parent:
            return await self._run_somewhere()
        key = (*[parent.get(field) for field in ("task_id", "generation", "claim_id", "start_id")],
               str(self.registry_entry.id), getattr(self.registry_entry, "_execution_generation", None))
        _waiter_counts[key] = _waiter_counts.get(key, 0) + 1
        operation = asyncio.create_task(self._run_somewhere())
        _consumer_operations.add(operation)
        def completed(task):
            _consumer_operations.discard(task)
            if not task.cancelled():
                task.exception()  # A detached consumer still has an observed outcome.
        operation.add_done_callback(completed)
        cancelled = False
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            remaining = _waiter_counts[key] - 1
            if remaining:
                _waiter_counts[key] = remaining
            else:
                del _waiter_counts[key]
                if cancelled and not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)

    async def _run_somewhere(self) -> Union[Model, SimstackResult, bool, None]:
        """Execute only the inline launch granted by Server; otherwise wait."""
        from simstack.core.context import execution_server
        with execution_server(getattr(self, "_execution_server", None)):
            if not self._owns_inline_nested_execution:
                return await self._wait_for_remote_completion()
            try:
                return await self.run_node_as_process()
            finally:
                reservation = getattr(self, "_inline_launch_reservation", None)
                if reservation is not None:
                    reservation.close()
                    self._inline_launch_reservation = None

    async def _recover_inline_launch(self, entry=None, *, observe_only=False):
        from simstack.runner_runtime.launch_claims import recover_launches
        entry = entry or self.registry_entry
        try:
            recovered = await recover_launches(context.server, task_id=entry.id, observed=entry)
        except OSError:
            if not observe_only or entry.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIME_OUT}:
                raise
            # Local observation is optional when reading an accepted outcome.
            # Revalidate its generation below; this is never proof of a stop.
            logger.warning("Task %s keeps its terminal outcome while local observation is unavailable", entry.id, exc_info=True)
            recovered = []
        changed_generation = (recovered and getattr(entry, "_execution_generation", None) is not None
            and getattr(recovered[0], "_execution_generation", None) != entry._execution_generation)
        if observe_only or not recovered or changed_generation:
            for pending in recovered:
                pending._launch_reservation.close()
            if getattr(entry, "_execution_local", False):
                # Recovery may only publish stop evidence, with no launch to
                # return. Read that outcome before deciding whether to wait.
                current = await context.server.tasks.get_task(entry.id)
                if (current is not None and getattr(entry, "_execution_generation", None) is not None
                        and getattr(current, "_execution_generation", None) != entry._execution_generation):
                    current = await context.server.tasks.get_outcome(entry, generation_only=True)
                if current is None:
                    raise RuntimeError(f"Task task_id: {entry.id} execution was superseded without an outcome")
                self.registry_entry = current
            return False
        self._dependency_server = context.server if self.parent_id else None
        self.registry_entry = recovered[0]
        self.parameters = self.registry_entry.parameters
        self._owns_inline_nested_execution = True
        self._inline_launch_reservation = self.registry_entry._launch_reservation
        token = getattr(self.registry_entry, "_execution_token", None)
        if token:
            from simstack.server_api import ServerAPI
            self._execution_server = ServerAPI.for_local_execution(
                context.server.requests.server_url, token,
                resource=self.registry_entry._execution_resource)
            self._execution_server.requests.bootstrap_data = context.server.requests.bootstrap_data
        return True

    async def _wait_for_remote_completion(
        self,
    ) -> Union[Model, SimstackResult, bool, None]:
        logger.info(f"entering wait for completion for task_id: {self.id}")
        observed = self.registry_entry
        try:
            while True:
                new_registry_entry = await context.server.tasks.get_task(self.id)
                if new_registry_entry is None:
                    raise RuntimeError(
                        f"Task task_id: {self.id} could not be found on Server"
                    )
                identity_changed = (
                    getattr(observed, "_execution_generation", None) is not None
                    and getattr(new_registry_entry, "_execution_generation", None)
                    != observed._execution_generation
                )
                if identity_changed:
                    # A rerun is a new computation, not this waiter's result.
                    new_registry_entry = await context.server.tasks.get_outcome(observed, generation_only=True)
                    if new_registry_entry is None:
                        raise RuntimeError(f"Task task_id: {self.id} execution was superseded without an outcome")
                else:
                    # Admission and Slurm requeue can change the physical
                    # attempt while preserving this computation's generation.
                    observed = new_registry_entry
                new_status = new_registry_entry.status
                if not identity_changed and getattr(new_registry_entry, "_execution_local", False):
                    if new_status == TaskStatus.RETRIEVED:
                        if await self._recover_inline_launch(new_registry_entry):
                            return await self._run_somewhere()
                    else:
                        await self._recover_inline_launch(new_registry_entry, observe_only=True)
                    new_registry_entry = self.registry_entry
                    new_status = new_registry_entry.status
                if (
                    new_status != TaskStatus.RUNNING
                    and new_status != TaskStatus.SLURM_RUNNING
                    and new_status != TaskStatus.SUBMITTED
                    and new_status != TaskStatus.SLURM_QUEUED
                    and new_status != TaskStatus.RETRIEVED
                ):
                    break

                print(f"Task task_id: {self.id} is waiting for results")
                await asyncio.sleep(5)

            self.registry_entry = new_registry_entry
            if new_status == TaskStatus.COMPLETED:
                logger.info(f"Task task_id: {self.id} completed remotely")
                return await self.load_results()
            error = sanitized_tail(
                new_registry_entry.error or f"terminated with status {new_status}",
                None,
                limit=None,
            )
            raise RuntimeError(
                f"Task task_id: {self.id} node: {self.name} failed with {error}"
            )
        except asyncio.CancelledError:
            from simstack.core.services.fail_children import fail_abandoned_child

            if self.parent_id is not None:
                await fail_abandoned_child(
                    self.registry_entry,
                    parent_id=self.parent_id,
                    parent_execution=self._parent_execution,
                    runner_pid=os.getpid(),
                )
            raise

    async def execute_node_locally(self, *, process_id: int | None = None) -> Union[Model, SimstackResult, None]:
        from simstack.runner_runtime.launch_claims import task_execution
        if self.registry_entry is None:
            raise ValueError("Task has no registry entry")
        with task_execution(self.registry_entry):
            return await self._execute_node_locally(process_id=process_id)

    async def _execute_node_locally(self, *, process_id: int | None = None) -> Union[Model, SimstackResult, None]:
        """
        Executes a specified node in the current context locally, either asynchronously or
        synchronously, managing task status updates, directory changes, and result persistence.

        This method handles the execution of a computational task represented as a "node". It
        manages the task's status transitions, file system operations for managing working
        directories, and handling output results, including their explicit publication through Server.
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
        await self.set_status(TaskStatus.RUNNING, process_id=process_id)
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
                if isinstance(result, bool):
                    self._execution_return_kind = "bool"
                elif isinstance(result, SimstackResult):
                    self._execution_return_kind = "multiple"
                elif isinstance(result, Model):
                    self._execution_return_kind = "model"
                else:
                    self._execution_return_kind = "none"
                self.registry_entry.return_kind = self._execution_return_kind
            except Exception as e:
                # Preserve the scientific error before publishing diagnostics.
                clean_error = sanitized_tail(
                    str(e),
                    None,
                    limit=None,
                )
                if self.registry_entry:
                    self.registry_entry.error = clean_error
                logger.error(
                    "Task task_id: %s node function error for node: %s msg: %s",
                    self.id,
                    self.name,
                    clean_error,
                )
                # Publish available diagnostics, in particular info_files.
                try:
                    await self.process_results(node_runner)
                except Exception:
                    logger.warning("Task %s diagnostic files could not be published", self.id, exc_info=True)
                raise
            finally:
                clear_task_id()

            self.registry_entry.completed_at = datetime.now()

            new_task_status, result = await self.process_results(result)
            self._execution_return_kind = self.registry_entry.return_kind

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
        except Exception as exc:
            if not self.registry_entry.error:
                self.registry_entry.error = sanitized_tail(str(exc), None, limit=None)
            try:
                await self.set_status(TaskStatus.FAILED)
            except Exception:
                logger.warning("Task %s failure awaits reconciliation", self.id, exc_info=True)
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
            self.registry_entry.return_kind = "none"
            logger.warning(f"Task task_id: {self.id} returned None")
            new_task_status = TaskStatus.FAILED  # result is None
        elif isinstance(result, bool):
            self.registry_entry.return_kind = "bool"
            if not result:
                self.registry_entry.error = "Node returned False"
                new_task_status = TaskStatus.FAILED
                result = None
        elif is_simstack_model(result) or isinstance(result, SimstackResult):
            if isinstance(result, SimstackResult):
                self.registry_entry.return_kind = "multiple"
                new_task_status = result.status
                if result.custom_name is not None:
                    self.custom_name = result.custom_name
                    self.registry_entry.custom_name = result.custom_name
                    object.__setattr__(self.registry_entry, "_outcome_custom_name", result.custom_name)

                for file_stack in result.info_files:
                    if file_stack:
                        if isinstance(file_stack, FileStack):
                            logger.info(
                                f"Task task_id: {self.id} saving info file: {file_stack.name} {file_stack.id}"
                            )
                            if self.registry_entry.info_files is None:
                                self.registry_entry.info_files = FileList()
                            await context.server.values.publish_value(file_stack)
                            self.registry_entry.info_files.append(file_stack)
                        else:
                            logger.error(
                                f"Task task_id: {self.id} cannot save info_file: FileStack expected but got {type(file_stack)}"
                            )
                    else:
                        logger.error(f"Task task_id: {self.id} saving info-file is NONE")
                        raise ValueError("saving info file is NONE")

                if result.error_message is not None and result.error_message != "":
                    clean_error = sanitized_tail(
                        result.error_message,
                        None,
                        limit=None,
                    )
                    self.registry_entry.error = clean_error
                    logger.error(
                        "Task task_id: %s returned with error: %s",
                        self.id,
                        clean_error,
                    )
                if result.message is not None and result.message != "":
                    self.registry_entry.message = result.message
                    logger.info(f"Task task_id: {self.id} message: {result.message}")
            else:
                self.registry_entry.return_kind = "model"
                if hasattr(result, "status"):
                    new_task_status = result.status
                elif hasattr(result, "task_status"):
                    new_task_status = result.task_status

            results_references, result_models = await process_result_helper(result, str(self.id))
            publication = None
            if result_models:
                result_models, publication = await context.server.values.publish_task_outputs(
                    result_models, execution=context.server.tasks.execution(self.registry_entry),
                    repository_revision=self.registry_entry.repository_revision)
            object.__setattr__(self.registry_entry, "_output_publication", publication)
            self.registry_entry.results_references = results_references
            self.registry_entry.status = new_task_status

            if isinstance(result, SimstackResult):
                for reference, value in zip(results_references, result_models):
                    # The immediate SimstackResult.files API remains a list;
                    # its stored result reference is the FileListModel wrapper.
                    setattr(result, reference.variable_name,
                            list(value) if reference.variable_name == "files" else value)
            elif len(results_references) == 1:
                result = result_models[0]
                self.registry_entry.return_kind = "model"

        else:
            self.registry_entry.return_kind = "none"
            logger.warning(
                f"Task task_id: {self.id} returned a result of type {type(result)} which is not a SimstackModel or a SimstackResult"
            )
            new_task_status = TaskStatus.FAILED
        return new_task_status, result

    async def set_status(self, status: TaskStatus, *, process_id: int | None = None) -> None:
        if self.registry_entry is None:
            raise ValueError("Task has no registry entry")
        if (
            status == TaskStatus.RUNNING
            and self.registry_entry.status == TaskStatus.TERMINATING
        ):
            raise RuntimeError("Terminated by user")
        if isinstance(status, TaskStatus):
            self.registry_entry.status = status
        else:
            logger.warning(f"Task task_id: {self.id} {status} is not a TaskStatus")
            self.registry_entry.status = TaskStatus(status)
        if status == TaskStatus.RUNNING:
            updated = await context.server.tasks.start_attempt(self.registry_entry,
                process_id=None if context.in_docker else process_id if process_id is not None else os.getpid())
            from simstack.runner_runtime.launch_claims import acknowledge_start
            acknowledge_start(context.server, self.registry_entry, updated)
        elif status == TaskStatus.COMPLETED:
            updated = await context.server.tasks.complete_attempt(self.registry_entry,
                return_kind=self.registry_entry.return_kind)
        elif status == TaskStatus.FAILED:
            updated = await context.server.tasks.fail_attempt(
                self.registry_entry, self.registry_entry.error or "Node execution failed",
                include_outputs=True,
                return_kind=getattr(self, "_execution_return_kind", None) or "exception",
            )
        else:
            raise ValueError(f"Unsupported execution transition: {status}")
        if updated is not None:
            self.registry_entry = updated
        logger.info(f"Task task_id: {self.id} {self.name} is set to {status}, id is: {self.id}")
        if status == TaskStatus.FAILED:
            from simstack.core.services.fail_children import fail_and_terminate_children

            await fail_and_terminate_children(self.registry_entry, runner_pid=os.getpid())


async def node_from_task(registry_entry: NodeRegistry) -> Union["Node", None]:
    """Reconstruct local code and inputs from a Server-authorized task DTO."""
    try:
        args = await context.server.values.resolve_task_inputs(registry_entry.id)
        wrapped = await import_function(
            registry_entry.func_mapping, context.server.values, task_id=registry_entry.id,
        )
        if wrapped is None:
            raise ValueError(f"Could not import function {registry_entry.func_mapping}")
        func = getattr(wrapped, "_inner", wrapped)
        declared_version = getattr(func, "_node_version", None)
        if ((registry_entry.status != TaskStatus.COMPLETED or registry_entry.parameters.force_rerun)
                and registry_entry.version is not None and registry_entry.version != declared_version):
            raise ValueError(
                f"Declared node version mismatch for {registry_entry.name}: "
                f"submitted {registry_entry.version!r}, worker {declared_version!r}"
            )
        node = Node(*args, func=func, is_async=asyncio.iscoroutinefunction(func),
            call_path=registry_entry.call_path, parameters=registry_entry.parameters,
            custom_name=registry_entry.custom_name, arg_hash=registry_entry.arg_hash,
            function_hash=registry_entry.function_hash,
            parent_id=registry_entry.parent_ids[0] if registry_entry.parent_ids else None)
        node.registry_entry = registry_entry
        if getattr(registry_entry, "_execution_token", None):
            from simstack.server_api import ServerAPI
            node._execution_server = ServerAPI.for_local_execution(
                context.server.requests.server_url, registry_entry._execution_token,
                resource=registry_entry._execution_resource)
            node._execution_server.requests.bootstrap_data = context.server.requests.bootstrap_data
            node._owns_inline_nested_execution = True
            node._inline_launch_reservation = registry_entry._launch_reservation
        return node
    except Exception as exc:
        error = sanitized_tail(str(exc), None, limit=None)
        updated = await context.server.tasks.fail_attempt(registry_entry, error, phase="prepare")
        if updated is not None:
            registry_entry.status = updated.status
            registry_entry.error = updated.error
        logger.error("Task %s could not load its code/inputs: %s", registry_entry.id, error)
        return None


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

    Cached-result identity is the node name and argument hash. Function identity
    is versioned by git rather than by hashing the function body.

    """

    def decorator(func: Callable[P, T]) -> Callable[..., T]:
        is_async = asyncio.iscoroutinefunction(func)

        setattr(func, "_is_node", True)
        setattr(func, "_node_version", version)
        # A new decoration defines a new implementation snapshot.
        if hasattr(func, "_node_code_version"):
            delattr(func, "_node_code_version")
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
                TaskStatus.RUNNING,
                TaskStatus.SLURM_RUNNING,
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
                current_registry_entry = await context.server.tasks.get_task(execution_node.registry_entry.id)
                if current_registry_entry is None:
                    raise RuntimeError(
                        f"Task task_id: {execution_node.id} node: {execution_node.name} registry entry disappeared"
                    )

                if (
                    current_registry_entry.status == TaskStatus.FAILED
                    and current_registry_entry.error
                ):
                    error = sanitized_tail(
                        current_registry_entry.error,
                        None,
                        limit=None,
                    )
                    raise RuntimeError(
                        f"task_id: {current_registry_entry.id} node: "
                        f"{current_registry_entry.name} failed with {error}"
                    )

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
                TaskStatus.RUNNING,
                TaskStatus.SLURM_RUNNING,
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
                if (
                    execution_node.registry_entry is not None
                    and execution_node.registry_entry.status == TaskStatus.FAILED
                    and execution_node.registry_entry.error
                ):
                    error = sanitized_tail(
                        execution_node.registry_entry.error,
                        None,
                        limit=None,
                    )
                    raise RuntimeError(
                        f"task_id: {execution_node.registry_entry.id} node: "
                        f"{execution_node.registry_entry.name} failed with {error}"
                    )
                return cast(T, result)
            if result is None or execution_node.status != TaskStatus.COMPLETED:
                if (
                    execution_node.registry_entry is not None
                    and execution_node.registry_entry.status == TaskStatus.FAILED
                    and execution_node.registry_entry.error
                ):
                    error = sanitized_tail(
                        execution_node.registry_entry.error,
                        None,
                        limit=None,
                    )
                    raise RuntimeError(
                        f"task_id: {execution_node.registry_entry.id} node: "
                        f"{execution_node.registry_entry.name} failed with {error}"
                    )



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
