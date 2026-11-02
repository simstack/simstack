import logging

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("node_claim")


async def claim_submitted_node(registry_entry: NodeRegistry) -> bool:
    """Automatically claim a submitted node for execution or submission."""
    if registry_entry.id is None:
        return False

    from simstack.core.definitions import DBType
    if getattr(context.db, "databae_type", None) == DBType.API:
        claimed = await context.db.task_mutation(registry_entry.id, "claim", {"generation": getattr(registry_entry, "_execution_generation", None)})
    else:
        claimed = await context.db.task_mutation(registry_entry.id, "claim")
    if claimed is None:
        logger.debug(
            "Task task_id: %s was already claimed before this runner could claim it",
            registry_entry.id,
        )
        return False

    registry_entry.status = TaskStatus.RETRIEVED
    if isinstance(claimed, dict) and claimed.get("_execution_claim_id"):
        object.__setattr__(registry_entry, "_execution_claim_id", claimed["_execution_claim_id"])
    return True
