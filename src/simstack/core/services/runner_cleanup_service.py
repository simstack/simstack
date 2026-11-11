"""Clean local cached files; persisted retention and metadata belong to Server."""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from simstack.core.context import context
from simstack.core.services.base_service import BaseService
from simstack.models.document.codec import decode_document_value

logger = logging.getLogger("NodeRunner")

class RunnerCleanupService(BaseService):
    def __init__(self, resource, interval=300):
        super().__init__("RunnerCleanup", resource, interval)

    async def execute(self):
        if os.environ.get("SIMSTACK_FILE_CACHE_CLEANUP_ENABLED", "1").lower() not in {"0", "false", "no"}:
            await self._cleanup_file_cache()

    async def _cleanup_file_cache(self):
        ttl_days = max(int(os.environ.get("SIMSTACK_FILE_CACHE_TTL_DAYS", "14")), 0)
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        workdir = Path(context.config.workdir).resolve()
        manifest = decode_document_value(await context.server.resource.cache_locations())
        for location in manifest.get("locations", []):
            resource = location.get("resource")
            resource = resource.get("value") if isinstance(resource, dict) else resource
            if (resource != str(context.config.resource)
                    or (location.get("location_type") or "local_path") != "local_path"
                    or (location.get("status") or "available") != "available"
                    or location.get("is_cached") is not True):
                continue
            accessed = location.get("last_accessed_at") or location.get("created_at")
            if isinstance(accessed, str):
                accessed = datetime.fromisoformat(accessed)
            if accessed is not None:
                if accessed.tzinfo is None:
                    accessed = accessed.replace(tzinfo=timezone.utc)
                if accessed > cutoff:
                    continue
            path = Path(location["path"])
            resolved = (path if path.is_absolute() else workdir / path).resolve()
            if resolved == workdir or workdir not in resolved.parents:
                logger.warning("Skipping cached file outside workdir: %s", resolved)
                continue
            try:
                from simstack.runner_runtime.file_cache import evict_cached_file
                if not evict_cached_file(resolved):
                    continue
            except OSError:
                logger.warning("Could not remove cached file %s", resolved, exc_info=True)
                continue
            await context.server.resource.cache_deleted(location["file_id"], location["path"],
                observed_instance_id=location.get("id"))
