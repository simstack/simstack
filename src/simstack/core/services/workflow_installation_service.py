"""Poll and report resource-local workflow installations through Server API."""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from simstack.core.context import context
from simstack.core.services.base_service import BaseService
from simstack.runner_api.errors import RunnerApiError
from simstack.runner_protocol.dto.workflows import RunnerWorkflowClaimRequestV1
from simstack.runner_runtime.workflow_installation import install_package

logger = logging.getLogger(__name__)


class WorkflowInstallationService(BaseService):
    def __init__(self, resource, interval: int, shutdown_event=None):
        super().__init__(
            "WorkflowInstallation", resource, interval, shutdown_event=shutdown_event
        )

    async def execute(self):
        for pending in await context.runner_api.pending_workflow_installations():
            installation_id = pending["installation_id"]
            request_id = uuid4().hex
            lease_seconds = RunnerWorkflowClaimRequestV1.model_fields[
                "lease_seconds"
            ].default
            try:
                claim = await context.runner_api.claim_workflow_installation(
                    installation_id, request_id=request_id, lease_seconds=lease_seconds
                )
            except RunnerApiError as exc:
                if exc.status_code == 409:
                    # Another supervisor may have claimed the same resource's
                    # pending job after this poll. This is normal contention.
                    continue
                raise
            descriptor = {
                "installation_id": installation_id,
                "package_id": claim["package_id"],
                "revision": claim["workflow_revision"],
                "runtime_profile": claim["runtime_profile"],
                "manifest": claim["manifest"],
            }
            binding = {
                "revision": descriptor["revision"],
                "claim_id": claim["claim_id"],
                "attempt": claim["attempt"],
            }
            try:
                profile = context.runner_adapter.workflow_installer(
                    descriptor["runtime_profile"]
                )
                await self._prepare_with_lease(
                    installation_id,
                    request_id,
                    lease_seconds,
                    binding,
                    install_package(
                        context.runner_api,
                        descriptor,
                        workdir=Path(context.config.workdir),
                        profile=profile,
                        claim_id=binding["claim_id"],
                        attempt=binding["attempt"],
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Workflow installation %s failed: %s",
                    installation_id,
                    type(exc).__name__,
                )
                await self._report(installation_id, binding, "failed", str(exc))
            else:
                await self._report(installation_id, binding, "ready")

    async def _prepare_with_lease(
        self, installation_id, request_id, seconds, binding, preparation
    ):
        async def renew():
            while True:
                await asyncio.sleep(seconds / 3)
                claim = await context.runner_api.claim_workflow_installation(
                    installation_id, request_id=request_id, lease_seconds=seconds
                )
                if any(
                    claim[key] != binding[expected]
                    for key, expected in (
                        ("claim_id", "claim_id"),
                        ("attempt", "attempt"),
                        ("workflow_revision", "revision"),
                    )
                ):
                    raise RunnerApiError(
                        "Workflow installation lease identity changed", status_code=409
                    )

        install = asyncio.create_task(preparation)
        renewal = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait(
                (install, renewal), return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                await renewal
            return await install
        finally:
            for task in (install, renewal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(install, renewal, return_exceptions=True)

    async def _report(self, installation_id, binding, status, error=None):
        try:
            await context.runner_api.report_workflow_installation(
                installation_id, **binding, status=status, error=error
            )
        except RunnerApiError as exc:
            if exc.status_code != 409:
                raise
            # Expired/replaced leases cannot publish a prepared package. The
            # durable Server job can be reclaimed; do not stop unrelated tasks.
            logger.info("Workflow installation %s claim changed", installation_id)
