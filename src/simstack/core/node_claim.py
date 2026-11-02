import logging

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("node_claim")


async def claim_submitted_node(registry_entry: NodeRegistry) -> bool:
    """Automatically claim a submitted node for execution or submission."""
    if registry_entry.id is None:
        return False

    claimed = await context.db.task_mutation(registry_entry.id, "claim")
    if claimed is None:
        logger.debug(
            "Task task_id: %s was already claimed before this runner could claim it",
            registry_entry.id,
        )
        return False

    registry_entry.status = TaskStatus.RETRIEVED
    return True
