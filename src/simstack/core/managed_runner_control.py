"""Task-claim coordination used only by server-managed runner containers."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from pymongo import ReturnDocument

from simstack.core.context import context

logger = logging.getLogger("managed_runner_control")


@asynccontextmanager
async def reserve_managed_runner_claim() -> AsyncIterator[bool]:
    """Prevent a managed runner from claiming work while it is stopping."""

    if os.environ.get("SIMSTACK_RUNNER_TYPE") != "managed":
        yield True
        return

    resource_name = str(context.config.resource)
    control_collection = context.db.get_collection("managed_runner_control")
    reserved = (
        await control_collection.find_one_and_update(
            {"_id": resource_name, "stopping": {"$ne": True}},
            {
                "$inc": {"claims_in_progress": 1},
                "$set": {"claims_updated_at": datetime.now(timezone.utc)},
            },
            return_document=ReturnDocument.AFTER,
        )
        is not None
    )
    if not reserved:
        yield False
        return

    try:
        yield True
    finally:
        await control_collection.update_one(
            {"_id": resource_name},
            {
                "$inc": {"claims_in_progress": -1},
                "$set": {"claims_updated_at": datetime.now(timezone.utc)},
            },
        )
