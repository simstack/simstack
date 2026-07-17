import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.models.files import FileStack
from simstack.models.parameters import Resource
from simstack.models.runner_model import RunnerEvent, RunnerType
from simstack.core.services.base_service import BaseService

logger = logging.getLogger("NodeRunner")


def _resource_to_str(resource: Any) -> str:
    if hasattr(resource, "resource_str"):
        return str(getattr(resource, "resource_str"))
    raw = getattr(resource, "__dict__", {}).get("value")
    if raw is not None:
        return str(raw)
    return str(resource)


def _cache_ttl_days() -> int:
    raw = os.environ.get("SIMSTACK_FILE_CACHE_TTL_DAYS")
    if raw is None:
        return 14
    try:
        value = int(raw)
    except ValueError:
        return 14
    return max(value, 0)


def _cache_cleanup_enabled() -> bool:
    return os.environ.get("SIMSTACK_FILE_CACHE_CLEANUP_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }


def _as_naive_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


class RunnerCleanupService(BaseService):
    """
    Service that cleans up old RunnerEvent logs and expired local file cache
    copies for the current resource.
    """

    def __init__(self, resource: Resource, interval: int = 300) -> None:
        # Default interval 5 minutes
        super().__init__("RunnerCleanup", resource, interval)

    async def execute(self) -> None:
        cutoff_time = datetime.now() - timedelta(minutes=30)

        # Find and delete events matching the criteria
        old_events = await context.db.find(
            RunnerEvent,
            (RunnerEvent.runner_type == RunnerType.RESOURCE_RUNNER)
            & (RunnerEvent.resource == self._resource)
            & (RunnerEvent.timestamp < cutoff_time),
        )

        if old_events:
            logger.info(
                f"Cleaning up {len(old_events)} old RunnerEvent logs for resource {self._resource}"
            )
            for event in old_events:
                await context.db.delete(event)

        if _cache_cleanup_enabled():
            await self._cleanup_file_cache()

    async def _cleanup_file_cache(self) -> None:
        ttl_days = _cache_ttl_days()
        cutoff_time = datetime.now() - timedelta(days=ttl_days)
        resource_name = _resource_to_str(self._resource)
        workdir = Path(context.config.workdir).resolve()

        file_stacks = await context.db.find_all(FileStack)
        updated_count = 0
        deleted_count = 0

        for file_stack in file_stacks:
            changed = False
            for location in file_stack.locations or []:
                if _resource_to_str(location.resource) != resource_name:
                    continue
                if getattr(location, "location_type", "local_path") != "local_path":
                    continue
                if getattr(location, "status", "available") != "available":
                    continue
                if not getattr(location, "is_cached", False):
                    continue

                accessed_at = _as_naive_datetime(
                    getattr(location, "last_accessed_at", None) or location.created_at
                )
                if accessed_at and accessed_at > cutoff_time:
                    continue

                path = Path(location.path)
                resolved_path = path if path.is_absolute() else workdir / path
                try:
                    resolved_path = resolved_path.resolve()
                    if (
                        workdir != resolved_path
                        and workdir not in resolved_path.parents
                    ):
                        logger.warning(
                            "Skipping cached FileInstance outside workdir: file_stack=%s path=%s",
                            file_stack.id,
                            resolved_path,
                        )
                        continue
                    if resolved_path.exists():
                        resolved_path.unlink()
                        deleted_count += 1
                    location.status = "deleted"
                    location.expires_at = datetime.now()
                    changed = True
                except Exception as exc:
                    logger.warning(
                        "Failed to clean cached FileInstance file_stack=%s path=%s: %s",
                        file_stack.id,
                        resolved_path,
                        exc,
                    )

            if changed:
                await context.db.save(file_stack)
                updated_count += 1

        if updated_count:
            logger.info(
                "Cleaned file cache for resource %s: marked %s FileStack(s), deleted %s file(s)",
                resource_name,
                updated_count,
                deleted_count,
            )
