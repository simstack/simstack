import logging

from pymongo import ReturnDocument

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.models import NodeRegistry

logger = logging.getLogger("node_claim")


async def claim_submitted_node(registry_entry: NodeRegistry) -> bool:
    """Atomically claim a submitted node for execution or submission."""
    if registry_entry.id is None:
        return False

    collection = context.db.engine.get_collection(NodeRegistry)
    claimed = await collection.find_one_and_update(
        {"_id": registry_entry.id, "status": TaskStatus.SUBMITTED.value},
        {"$set": {"status": TaskStatus.RETRIEVED.value}},
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        logger.debug(
            "Task task_id: %s was already claimed before this runner could claim it",
            registry_entry.id,
        )
        return False

    registry_entry.status = TaskStatus.RETRIEVED
    return True
