import asyncio
import http.client
import logging
from threading import Event
from pathlib import Path
from typing import Any

from simstack.core.context import context
from simstack.core.services.base_service import BaseService
from simstack.models.parameters import Resource
from simstack.runner_api.file_transfer import (
    FileTransferError,
    SupervisorFileTransferClient,
    resolve_instance_path,
)
from simstack.runner_protocol import FileTransferRole, FileTransferStatus

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
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("File transfer max_concurrent must be a positive integer")
        super().__init__(
            "FileTransfer", resource, interval, shutdown_event=shutdown_event
        )
        self._log_traceback_on_failure = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running_tasks: set[asyncio.Task[bool]] = set()
        self._active_tasks: set[asyncio.Task[bool]] = set()
        self._upload_stop = Event()
        self._client: SupervisorFileTransferClient | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._upload_stop.clear()
        return super().start()

    async def stop(self) -> None:
        # asyncio cancellation cannot stop to_thread I/O. Signal the transport,
        # cancel only queued work, and await active threads until they close.
        self._upload_stop.set()
        await super().stop()
        for task in self._running_tasks - self._active_tasks:
            if not task.done():
                task.cancel()
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks, return_exceptions=True)
        self._running_tasks.clear()

    def _get_client(self) -> SupervisorFileTransferClient:
        if self._client is not None:
            return self._client
        self._client = SupervisorFileTransferClient.from_context()
        return self._client

    async def execute(self) -> None:
        if self._upload_stop.is_set():
            return
        completed_tasks = {task for task in self._running_tasks if task.done()}
        for task in completed_tasks:
            try:
                await task
            except Exception as exc:
                logger.exception("File transfer task completed with error: %s", exc)
            self._running_tasks.remove(task)

        client = self._get_client()
        transfers = await asyncio.to_thread(
            client.list_transfers,
            role=FileTransferRole.SOURCE,
            status=FileTransferStatus.CREATED,
            limit=10,
        )
        if self._upload_stop.is_set():
            return

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
        self, client: SupervisorFileTransferClient, transfer: dict[str, Any]
    ) -> bool:
        async with self._semaphore:
            if self._upload_stop.is_set():
                return False
            task = asyncio.current_task()
            self._active_tasks.add(task)
            try:
                return await asyncio.to_thread(
                    self._upload_transfer, client, transfer, stop_event=self._upload_stop
                )
            finally:
                self._active_tasks.discard(task)

    def _upload_transfer(
        self, client: SupervisorFileTransferClient, transfer: dict[str, Any], *,
        stop_event: Event | None = None,
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

        try:
            path = resolve_instance_path(str(source_path), Path(context.config.workdir))
            if not path.is_file():
                raise FileNotFoundError(f"Transfer source file is unavailable: {source_path}")
        except (FileTransferError, FileNotFoundError) as exc:
            client.fail_transfer(
                transfer_id,
                error_message=str(exc),
                error_code="SOURCE_FILE_UNAVAILABLE",
            )
            return False
        try:
            client.upload_file(transfer_id, path, stop_event=stop_event)
            logger.info("Uploaded FileStack transfer %s from %s", transfer_id, path)
            return True
        except FileTransferError as exc:
            logger.warning("File transfer upload failed for %s: %s", transfer_id, exc)
            if exc.status_code is None or exc.status_code >= 500 or exc.status_code == 423:
                logger.info(
                    "Leaving FileStack transfer %s recoverable for a later poll",
                    transfer_id,
                )
                return False
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
        except (OSError, http.client.HTTPException) as exc:
            logger.warning(
                "File transfer transport failed for %s; leaving it recoverable: %s",
                transfer_id,
                exc,
            )
            return False
