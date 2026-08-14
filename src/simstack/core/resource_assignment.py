import logging
from dataclasses import dataclass
from typing import Optional

from odmantic import AIOEngine

from simstack.models import NodeRegistry
from simstack.models.parameters import Parameters, Resource, SlurmParameters
from simstack.models.resource_assignment import (
    ResourceAssignmentRule,
    SlurmParametersPatch,
)
from simstack.util.db import Database

logger = logging.getLogger("resource_assignment")

SLURM_QUEUE_NAME = "slurm-queue"


@dataclass
class ResourceAssignmentResolution:
    parameters: Parameters
    normalized_call_path: str
    matched_rule: Optional[ResourceAssignmentRule] = None


def normalize_call_path(call_path: Optional[str]) -> str:
    return (call_path or "").strip().lstrip(".")


def _normalize_queue(queue: Optional[str]) -> str:
    normalized = (queue or "").strip()
    return normalized or "default"


def _is_slurm_queue(queue: Optional[str]) -> bool:
    return _normalize_queue(queue).lower() == SLURM_QUEUE_NAME


def _parameters_resource_str(parameters: Parameters) -> str:
    raw = parameters.__dict__.get("resource")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    inner = getattr(raw, "__dict__", {}) or {}
    return str(inner.get("value") or "")


def _keeps_slurm_allocation(parameters: Parameters) -> bool:
    """Cloud VMs size from SlurmParameters; do not strip mem/CPU for those jobs."""
    queue = _normalize_queue(getattr(parameters, "queue", None)).lower()
    if queue in {SLURM_QUEUE_NAME, "cloud"}:
        return True
    return _parameters_resource_str(parameters).lower() == "cloud"


def _clone_parameters(parameters: Optional[Parameters]) -> Parameters:
    if isinstance(parameters, Parameters):
        return parameters.model_copy(deep=True)
    return Parameters()


def _merge_slurm_patch(
    patch: Optional[SlurmParametersPatch],
) -> SlurmParameters:
    if patch is None:
        return SlurmParameters()
    return SlurmParameters(**patch.model_dump(exclude_none=True))


def empty_slurm_parameters() -> SlurmParameters:
    cleared_values: dict[str, object] = {}
    for field_name, field_info in SlurmParameters.model_fields.items():
        if field_info.default_factory is not None:
            cleared_values[field_name] = field_info.default_factory()
        else:
            cleared_values[field_name] = None
    return SlurmParameters.model_validate(cleared_values)


def _apply_assignment_patch(
    base_parameters: Parameters,
    rule: Optional[ResourceAssignmentRule],
) -> Parameters:
    effective = _clone_parameters(base_parameters)
    if rule is None:
        return effective

    if rule.resource_str:
        effective.resource = Resource(value=rule.resource_str)
    if rule.queue is not None:
        effective.queue = _normalize_queue(rule.queue)
    if rule.slurm_parameters_patch:
        effective.slurm_parameters = _merge_slurm_patch(
            SlurmParametersPatch.model_validate(rule.slurm_parameters_patch),
        )

    return effective


def normalize_and_validate_effective_parameters(
    parameters: Optional[Parameters],
) -> None:
    if parameters is None:
        return

    if not _is_slurm_queue(getattr(parameters, "queue", None)):
        if _keeps_slurm_allocation(parameters):
            return
        parameters.slurm_parameters = empty_slurm_parameters()
        return

    slurm_parameters = getattr(parameters, "slurm_parameters", None)
    if slurm_parameters is None:
        raise ValueError('Slurm queue requires "slurm_parameters".')

    fields_set: set[str] = getattr(slurm_parameters, "model_fields_set", set())
    has_nodes = "nodes" in fields_set and slurm_parameters.nodes is not None
    has_tasks = "tasks" in fields_set and slurm_parameters.tasks is not None
    has_tasks_per_node = (
        "tasks_per_node" in fields_set and slurm_parameters.tasks_per_node is not None
    )
    uses_default_nodes = (
        not has_nodes
        and not has_tasks
        and not has_tasks_per_node
        and slurm_parameters.nodes is not None
    )

    if uses_default_nodes:
        has_nodes = True
    elif not has_nodes:
        slurm_parameters.nodes = None
    if not has_tasks:
        slurm_parameters.tasks = None
    if not has_tasks_per_node:
        slurm_parameters.tasks_per_node = None

    if has_tasks and has_tasks_per_node:
        raise ValueError(
            'Slurm parameters conflict: use either "tasks" or "tasks_per_node".'
        )
    if not has_nodes and not has_tasks:
        raise ValueError('Slurm requires at least one of "nodes" or "tasks".')


def _select_matching_rule(
    normalized_call_path: str,
    rules: list[ResourceAssignmentRule],
) -> Optional[ResourceAssignmentRule]:
    enabled_rules = [rule for rule in rules if getattr(rule, "enabled", True)]
    matching_rules = [
        rule
        for rule in enabled_rules
        if ResourceAssignmentRule.matches_call_path(
            rule.regex_pattern, normalized_call_path
        )
    ]
    if not matching_rules:
        return None

    highest_score = max(
        ResourceAssignmentRule.pattern_specificity_score(rule.regex_pattern)
        for rule in matching_rules
    )
    top_rules = [
        rule
        for rule in matching_rules
        if ResourceAssignmentRule.pattern_specificity_score(rule.regex_pattern)
        == highest_score
    ]
    if len(top_rules) > 1:
        rule_names = ", ".join(sorted(rule.name for rule in top_rules))
        raise ValueError(
            "Ambiguous resource assignment: "
            f"multiple equally specific rules matched call_path '{normalized_call_path}': {rule_names}"
        )
    return top_rules[0]


async def resolve_resource_assignment(
    db: Database,
    *,
    call_path: Optional[str],
    base_parameters: Optional[Parameters],
    parent_parameters: Optional[Parameters] = None,
) -> ResourceAssignmentResolution:
    normalized_call_path = normalize_call_path(call_path)
    effective_base = _clone_parameters(base_parameters)

    if not normalized_call_path:
        normalize_and_validate_effective_parameters(effective_base)
        return ResourceAssignmentResolution(
            parameters=effective_base,
            normalized_call_path=normalized_call_path,
            matched_rule=None,
        )

    rules = await db.find(ResourceAssignmentRule)
    matched_rule = _select_matching_rule(normalized_call_path, list(rules))
    effective_parameters = _apply_assignment_patch(effective_base, matched_rule)
    normalize_and_validate_effective_parameters(effective_parameters)

    if matched_rule is not None:
        logger.info(
            "Applied resource assignment rule '%s' to call_path '%s'",
            matched_rule.name,
            normalized_call_path,
        )

    return ResourceAssignmentResolution(
        parameters=effective_parameters,
        normalized_call_path=normalized_call_path,
        matched_rule=matched_rule,
    )


async def apply_resource_assignment_to_node_registry(
    db: Database,
    node_registry: NodeRegistry,
    *,
    parent_parameters: Optional[Parameters] = None,
) -> ResourceAssignmentResolution:
    resolution = await resolve_resource_assignment(
        db,
        call_path=getattr(node_registry, "call_path", None),
        base_parameters=getattr(node_registry, "parameters", None),
        parent_parameters=parent_parameters,
    )

    node_registry.parameters = resolution.parameters
    if resolution.matched_rule is None:
        node_registry.assignment_rule_id = None
        node_registry.assignment_rule_name = None
        node_registry.assignment_pattern = None
    else:
        node_registry.assignment_rule_id = str(resolution.matched_rule.id)
        node_registry.assignment_rule_name = resolution.matched_rule.name
        node_registry.assignment_pattern = resolution.matched_rule.regex_pattern

    return resolution
