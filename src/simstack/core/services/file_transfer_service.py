import asyncio
import logging
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.core.services.base_service import BaseService
from simstack.models.parameters import Resource
from simstack.util.file_transfer_client import (
    FileTransferClient,
    FileTransferError,
    resolve_instance_path,
)

logger = logging.getLogger("NodeRunner")


class FileTransferService(BaseService):
    """Poll the SimStack server for hidden file transfer uploads assigned to this runner."""

    def __init__(
        self,
        resource: Resource,
        interval: int = 10,
        max_concurrent: int = 2,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__(
            "FileTransfer", resource, interval, shutdown_event=None
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks: set[asyncio.Task[bool]] = set()
        self._client: FileTransferClient | None = None
        self._configuration_checked = False

    def _get_client(self) -> FileTransferClient | None:
        if self._client is not None:
            return self._client
        if self._configuration_checked:
            return None
        self._configuration_checked = True
        self._client = FileTransferClient.from_context(required=False)
        if self._client is None:
            logger.info(
                "File transfer service disabled; SIMSTACK_SERVER_URL or SIMSTACK_RUNNER_TOKEN is not configured."
            )
        return self._client

    async def execute(self) -> None:
        completed_tasks = {task for task in self._running_tasks if task.done()}
        for task in completed_tasks:
            try:
                await task
            except Exception as exc:
                logger.exception("File transfer task completed with error: %s", exc)
            self._running_tasks.remove(task)

        client = self._get_client()
        if client is None:
            return

        try:
            transfers = client.list_transfers(role="source", status="created", limit=10)
        except Exception as exc:
            logger.warning("Unable to poll file transfers: %s", exc)
            raise exc

        for transfer in transfers:
            transfer_id = str(transfer.get("transfer_id") or "")
            if not transfer_id:
                continue
            if any(
                getattr(task, "transfer_id", None) == transfer_id
                for task in self._running_tasks
            ):
                continue
            task = asyncio.create_task(self._run_with_semaphore(client, transfer))
            setattr(task, "transfer_id", transfer_id)
            self._running_tasks.add(task)

    async def _run_with_semaphore(
        self, client: FileTransferClient, transfer: dict[str, Any]
    ) -> bool:
        async with self._semaphore:
            return await asyncio.to_thread(self._upload_transfer, client, transfer)

    def _upload_transfer(
        self, client: FileTransferClient, transfer: dict[str, Any]
    ) -> bool:
        transfer_id = str(transfer.get("transfer_id"))
        source_path = transfer.get("source_path")
        if not source_path:
            client.fail_transfer(
                transfer_id,
                error_message="Transfer is missing source_path metadata.",
                error_code="SOURCE_PATH_MISSING",
            )
            return False

        path = resolve_instance_path(str(source_path), Path(context.config.workdir))
        try:
            client.upload_file(transfer_id, path)
            logger.info("Uploaded FileStack transfer %s from %s", transfer_id, path)
            return True
        except FileTransferError as exc:
            logger.warning("File transfer upload failed for %s: %s", transfer_id, exc)
            try:
                client.fail_transfer(
                    transfer_id,
                    error_message=str(exc),
                    error_code="SOURCE_UPLOAD_FAILED",
                )
            except Exception:
                logger.exception(
                    "Failed to report failed file transfer %s", transfer_id
                )
            return False
