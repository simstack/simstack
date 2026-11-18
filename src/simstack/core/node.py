import asyncio
import functools
import inspect
import logging
import os
from typing import (
    Any,
    Callable,
    Optional,
    ParamSpec,
    TypeVar,
    Union,
    cast,
    overload,
)

import coolname  # type: ignore[import-untyped]
import nest_asyncio  # type: ignore[import-untyped]

from simstack.core import node_execution, node_outputs
from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node_argument_hash import (
    _HashableModel as _HashableModel,
    compute_arg_hash,
    hashable_inputs as hashable_inputs,
    hashable_value as hashable_value,
)
from simstack.core.node_submission import (
    docker_image_for_node as docker_image_for_node,
    normalize_docker_image as normalize_docker_image,
    process_is_in_docker as process_is_in_docker,
    should_handoff_nested_execution as should_handoff_nested_execution,
    submit_invocation,
)
from simstack.core.simstack_result import SimstackResult
from simstack.models import NodeRegistry, Parameters
from simstack.models.document import Model, ObjectId
from simstack.models.parameters import Queue
from simstack.runner_runtime import node_process
from simstack.util.importer import import_function
from simstack.util.sanitized_output import sanitized_tail

logger = logging.getLogger("Node")

# Concurrent calls in one scientific parent are separate consumers of its
# Server dependency. Keep an owned launch alive while any call still awaits it.
_waiter_counts: dict[tuple, int] = {}
_consumer_operations: set[asyncio.Task] = set()

nest_asyncio.apply()

T = TypeVar("T")


def default_name_generator() -> str:
    return str("-".join(coolname.generate(2)))


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
        self._recovery_of = kwargs.pop("recovery_of", None)
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
        return await submit_invocation(self, context, arg_hash)

    async def get_node_registry(self) -> TaskStatus:
        self._arg_hash = compute_arg_hash(self._args)
        self._function_hash = ""
        await self.make_registry_entry("", self._arg_hash)
        return self.registry_entry.status

    async def load_results(
        self, return_kind: str | None = None
    ) -> Union[Model, SimstackResult, bool, None]:
        """Restore the committed task outputs using their recorded return kind."""
        return await node_outputs.load_results(self, context, return_kind)

    async def run_node_as_process(
        self,
    ) -> Union[Model, SimstackResult, bool, None]:
        """Run this task in an isolated child and restore its persisted result."""
        return await node_process.run_node_as_process(self, context)

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
        return await node_execution.execute_node_locally(self, context, process_id=process_id)

    async def process_results(self, result: Any) -> tuple[TaskStatus, Any]:
        """Publish the scientific outputs and prepare their registry references."""
        return await node_outputs.publish_results(self, context, result)

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
