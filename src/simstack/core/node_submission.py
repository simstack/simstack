"""Publish an invocation and resolve cache reuse, placement and ancestry."""

import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from simstack.core.context import GlobalState, context
from simstack.core.definitions import TaskStatus
from simstack.core.node_argument_hash import compute_arg_hash
from simstack.models import NamedDataReference, NodeRegistry, Parameters
from simstack.models.document import Model, ObjectId
from simstack.models.parameters import Queue

if TYPE_CHECKING:
    from simstack.core.node import Node

logger = logging.getLogger("Node")

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


async def submit_invocation(
    node: "Node", context: GlobalState, arg_hash: str
) -> NodeRegistry:
    """Publish an invocation; Server resolves cache, lineage and routing."""
    from simstack.runner_runtime.launch_claims import (
        current_execution,
        finish,
        receipt,
        reserve_launch,
    )

    mapping = context.node_mappings.get_by_name(node.name)
    if mapping is None:
        raise ValueError(f"Could not find function mapping for name: {node.name}")
    for arg in node._args:
        if not isinstance(arg, Model):
            raise ValueError(f"{type(arg).__name__} is not a SimStack Model")
    node.parameters.recompute_artifacts = bool(node.recompute_artifacts)
    invocation = {
        "parent_execution": current_execution() if node.parent_id else None,
        "name": node.name,
        "function_mapping": mapping.function_mapping,
        "version": getattr(node._func, "_node_version", None),
        "repository_revision": None if node.parent_id else getattr(mapping, "repository_revision", None),
        "is_async": node.is_async,
        "arg_hash": arg_hash,
        "call_path": node.call_path,
        "parameters": node.parameters.model_dump(mode="json", exclude_unset=True),
        "custom_name": node.custom_name,
        "recompute": node.recompute_artifacts,
    }
    if node._recovery_of is not None:
        invocation["recovery_of"] = {
            "task_id": str(node._recovery_of.id),
            "generation": node._recovery_of._execution_generation,
        }
    if node.parent_id and not invocation["parent_execution"]:
        raise RuntimeError("A child invocation requires its parent's execution identity")
    node._parent_execution = dict(invocation["parent_execution"]) if node.parent_id else None
    node._dependency_server = context.server if node.parent_id else None
    resolved = await context.server.tasks.resolve_invocation(invocation, request_id=str(uuid4()))
    from simstack.server_api.scientific_values import contains_file_values
    prepared = None
    cached = None
    if contains_file_values(node._args):
        values, prepared = await context.server.values.preview_task_inputs(
            node._args, repository_revision=resolved["repository_revision"])
        invocation["arg_hash"] = compute_arg_hash(values)
        cached = await context.server.tasks.reuse_invocation(invocation, request_id=str(uuid4()))
    if cached is None or cached["task"] is None:
        publication_options = {"prepared": prepared} if prepared is not None else {}
        values, input_publication = await context.server.values.publish_task_inputs(
            node._args, repository_revision=resolved["repository_revision"], **publication_options)
        # Recheck the accepted snapshot after a miss: object metadata may
        # have changed since preview. Admission always uses its final hash.
        invocation["arg_hash"] = compute_arg_hash(values)
        cached = await context.server.tasks.reuse_invocation(invocation, request_id=str(uuid4()))
    # Serializers and resolution hooks can normalize values. Cache lookup
    # and admission both use a resolved scientific snapshot's hash.
    node._args = values
    node._arg_hash = invocation["arg_hash"]
    if cached["task"] is not None:
        node.registry_entry = cached["task"]
        node.parameters = node.registry_entry.parameters
        node.recompute_artifacts = cached["recompute_artifacts"]
        node._owns_inline_nested_execution = False
        if node.registry_entry.status in {TaskStatus.RETRIEVED, TaskStatus.SUBMITTED}:
            await node._recover_inline_launch()
        elif getattr(node.registry_entry, "_execution_local", False):
            await node._recover_inline_launch(observe_only=True)
        if cached["disposition"] == "reused_complete" and node.recompute_artifacts:
            from simstack.core.recompute_artifacts import recompute_artifacts
            await recompute_artifacts(node.registry_entry)
        return node.registry_entry
    effective = Parameters.model_validate(cached["parameters"])
    logical_self = cached["logical_self"]
    # Physical container/VM compatibility belongs to the actual launcher.
    # Cache/rule resolution remains Server-owned, so evaluate the original
    # handoff guards against its effective route before requesting a claim.
    inline = bool(node.parent_id and logical_self)
    if not inline:
        same_resource = str(effective.resource) == str(context.config.resource)
        inline = bool((logical_self or (same_resource and effective.queue == Queue.DEFAULT))
                      and not (not logical_self and _explicit_cloud_child_needs_own_vm(effective))
                      and not should_handoff_nested_execution(effective, node.name))
    local_root = node.parent_id is None and context.server.requests.authorization == "user" and inline
    inline = inline and (node.parent_id is not None or local_root)
    names = list(inspect.signature(node._func).parameters)
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
    if node.parent_id or local_root:
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
            if node.parent_id else context.server.tasks.submit_root(payload, request_id=request_id)
        )
        node.registry_entry = response["task"]
        if response.get("local_execution_token"):
            from simstack.server_api import ServerAPI
            node._execution_server = ServerAPI.for_local_execution(
                context.server.requests.server_url, response["local_execution_token"],
                resource=node.registry_entry._execution_resource)
            node._execution_server.requests.bootstrap_data = context.server.requests.bootstrap_data
        node.parameters = node.registry_entry.parameters
        node.recompute_artifacts = response["recompute_artifacts"]
        disposition = response["disposition"]
        node._owns_inline_nested_execution = disposition == "launch_inline"
        if node._owns_inline_nested_execution:
            if reservation is None:
                raise RuntimeError("An inline launch requires its publisher reservation")
            if str(node.registry_entry.id) != requested_task_id:
                raise RuntimeError("Server returned a different task for this inline publication")
            # Atomically replace the pending receipt with its canonical
            # claim. Never unlink it between publication and launch.
            intent = receipt(context.server, node.registry_entry.id, begin=True,
                revision=node.registry_entry.repository_revision,
                generation=getattr(node.registry_entry, "_execution_generation", None),
                inline=True, claim_id=getattr(node.registry_entry, "_execution_claim_id"),
                publication=payload, request_id=request_id)
            finish(context.server, intent, claimed=True)
            node._inline_launch_reservation = reservation
            object.__setattr__(node.registry_entry, "_launch_receipt", intent)
            reservation = None
        elif intent is not None:
            finish(context.server, intent, claimed=False)
        if disposition == "reused_complete" and node.recompute_artifacts:
            from simstack.core.recompute_artifacts import recompute_artifacts
            await recompute_artifacts(node.registry_entry)
        return node.registry_entry
    finally:
        if reservation is not None:
            reservation.close()
