from __future__ import annotations

import http.client
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import urlencode, urlparse

from simstack.util.file_hashing import hash_file

logger = logging.getLogger(__name__)


class FileTransferError(RuntimeError):
    """Raised when the SimStack file transfer API cannot satisfy a request."""


@dataclass
class DownloadResult:
    path: Path
    size_bytes: int
    checksum_sha256: str


class FileTransferClient:
    """
    Small standard-library HTTP client for runner-to-server file transfer.

    It intentionally avoids adding a mandatory requests/httpx dependency to the
    runner environment while still streaming upload and download bodies.
    """

    chunk_size = 1024 * 1024

    def __init__(
        self,
        *,
        server_url: str,
        runner_token: str,
        timeout_seconds: int = 60,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.runner_token = runner_token
        self.timeout_seconds = timeout_seconds
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid SIMSTACK_SERVER_URL: {server_url!r}")
        self._parsed = parsed

    @classmethod
    def from_context(cls, *, required: bool = True) -> "FileTransferClient | None":
        """
        Build a client from environment variables or context config attributes.

        Environment variables are preferred because runner tokens are machine
        credentials and should not be stored in FileStack/FileInstance objects.
        """
        try:
            from simstack.core.context import context

            config = getattr(context, "config", None)
        except Exception:
            config = None

        server_url = (
            os.environ.get("SIMSTACK_SERVER_URL")
            or getattr(config, "server_url", None)
            or getattr(config, "simstack_server_url", None)
        )
        runner_token = (
            os.environ.get("SIMSTACK_RUNNER_TOKEN")
            or getattr(config, "runner_token", None)
            or getattr(config, "simstack_runner_token", None)
        )
        timeout_seconds = int(os.environ.get("SIMSTACK_FILE_TRANSFER_REQUEST_TIMEOUT_SECONDS", "60"))

        if not server_url or not runner_token:
            if required:
                raise FileTransferError(
                    "Remote FileStack transfer requires SIMSTACK_SERVER_URL and "
                    "SIMSTACK_RUNNER_TOKEN in the runner environment."
                )
            return None

        return cls(
            server_url=str(server_url),
            runner_token=str(runner_token),
            timeout_seconds=timeout_seconds,
        )

    def create_transfer(
        self,
        *,
        file_stack_id: str,
        source_file_instance_id: str | None,
        source_resource_name: str | None,
        target_resource_name: str,
        request_type: str = "runner_to_runner",
    ) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            "/api/file-transfers",
            {
                "file_stack_id": file_stack_id,
                "source_file_instance_id": source_file_instance_id,
                "source_resource_name": source_resource_name,
                "target_resource_name": target_resource_name,
                "request_type": request_type,
            },
        )

    def get_transfer(self, transfer_id: str) -> Dict[str, Any]:
        return self._json_request("GET", f"/api/file-transfers/{transfer_id}")

    def list_transfers(
        self,
        *,
        role: str = "source",
        status: str | None = "created",
        limit: int = 20,
    ) -> list[Dict[str, Any]]:
        query = {"role": role, "limit": str(limit)}
        if status:
            query["status"] = status
        response = self._json_request("GET", f"/api/file-transfers?{urlencode(query)}")
        transfers = response.get("transfers", [])
        return transfers if isinstance(transfers, list) else []

    def fail_transfer(self, transfer_id: str, *, error_message: str, error_code: str | None = None) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            f"/api/file-transfers/{transfer_id}/fail",
            {"error_message": error_message, "error_code": error_code},
        )

    def wait_until_uploaded(
        self,
        transfer_id: str,
        *,
        timeout_seconds: int | None = None,
        poll_interval_seconds: float | None = None,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + float(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("SIMSTACK_FILE_TRANSFER_TIMEOUT_SECONDS", "3600")
        )
        interval = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else os.environ.get("SIMSTACK_FILE_TRANSFER_POLL_SECONDS", "5")
        )

        last_status = "unknown"
        while time.monotonic() < deadline:
            transfer = self.get_transfer(transfer_id)
            last_status = str(transfer.get("status", "unknown"))
            if last_status in {"uploaded", "target_downloading", "completed"}:
                return transfer
            if last_status in {"failed", "expired"}:
                raise FileTransferError(
                    f"Transfer {transfer_id} failed with status {last_status}: "
                    f"{transfer.get('error_message')}"
                )
            time.sleep(max(interval, 0.1))

        raise FileTransferError(
            f"Timed out waiting for transfer {transfer_id} to upload; last status was {last_status}."
        )

    def upload_file(self, transfer_id: str, path: Path) -> Dict[str, Any]:
        path = Path(path)
        if not path.is_file():
            raise FileTransferError(f"Transfer source file does not exist: {path}")

        size_bytes = path.stat().st_size
        checksum = hash_file(path)
        conn = self._connection()
        try:
            conn.putrequest("PUT", self._path(f"/api/file-transfers/{transfer_id}/upload"))
            for key, value in self._auth_headers().items():
                conn.putheader(key, value)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size_bytes))
            conn.putheader("X-File-Size", str(size_bytes))
            conn.putheader("X-Checksum-SHA256", checksum)
            conn.endheaders()

            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(self.chunk_size), b""):
                    conn.send(chunk)

            return self._parse_response(conn.getresponse())
        finally:
            conn.close()

    def download_file(self, transfer_id: str, target_path: Path) -> DownloadResult:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connection()
        temp_name = None
        try:
            conn.request(
                "GET",
                self._path(f"/api/file-transfers/{transfer_id}/download"),
                headers=self._auth_headers(),
            )
            response = conn.getresponse()
            if response.status >= 400:
                self._raise_response(response)

            import hashlib

            sha256 = hashlib.sha256()
            size_bytes = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(target_path.parent),
                prefix=f".{target_path.name}.",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    temp_file.write(chunk)
                    sha256.update(chunk)
                    size_bytes += len(chunk)

            temp_path = Path(temp_name)
            temp_path.replace(target_path)
            return DownloadResult(
                path=target_path,
                size_bytes=size_bytes,
                checksum_sha256=sha256.hexdigest(),
            )
        except Exception:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to clean temporary download %s", temp_name)
            raise
        finally:
            conn.close()

    def complete_transfer(
        self,
        *,
        transfer_id: str,
        target_resource_name: str,
        target_path: str,
        size_bytes: int,
        checksum_sha256: str,
    ) -> Dict[str, Any]:
        return self._json_request(
            "POST",
            f"/api/file-transfers/{transfer_id}/complete",
            {
                "target_resource_name": target_resource_name,
                "target_path": target_path,
                "size_bytes": size_bytes,
                "checksum_sha256": checksum_sha256,
            },
        )

    def _json_request(self, method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._auth_headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connection()
        try:
            conn.request(method, self._path(path), body=body, headers=headers)
            return self._parse_response(conn.getresponse())
        finally:
            conn.close()

    def _connection(self) -> http.client.HTTPConnection:
        connection_cls = http.client.HTTPSConnection if self._parsed.scheme == "https" else http.client.HTTPConnection
        return connection_cls(self._parsed.netloc, timeout=self.timeout_seconds)

    def _path(self, path: str) -> str:
        base_path = self._parsed.path.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base_path}{path}" if base_path else path

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.runner_token}"}

    def _parse_response(self, response: http.client.HTTPResponse) -> Dict[str, Any]:
        if response.status >= 400:
            self._raise_response(response)
        payload = response.read()
        if not payload:
            return {}
        content_type = response.getheader("Content-Type") or ""
        if "application/json" not in content_type:
            return {"raw": payload.decode("utf-8", errors="replace")}
        return json.loads(payload.decode("utf-8"))

    def _raise_response(self, response: http.client.HTTPResponse) -> None:
        payload = response.read()
        detail = payload.decode("utf-8", errors="replace") if payload else response.reason
        raise FileTransferError(f"File transfer API returned HTTP {response.status}: {detail}")


def path_for_file_instance(path: Path, workdir: Path) -> str:
    resolved_path = Path(path).resolve()
    resolved_workdir = Path(workdir).resolve()
    try:
        return str(resolved_path.relative_to(resolved_workdir))
    except ValueError:
        return str(resolved_path)


def resolve_instance_path(raw_path: str, workdir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path(workdir) / path


def resource_name(value: Any) -> str:
    raw = getattr(value, "__dict__", {}).get("value")
    if raw is not None:
        return str(raw)
    return str(value)


def first_available_remote_location(locations: Iterable[Any], local_resource: Any) -> Any | None:
    local_resource_str = resource_name(local_resource)
    for location in locations:
        if resource_name(getattr(location, "resource", "")) == local_resource_str:
            continue
        if getattr(location, "status", "available") != "available":
            continue
        return location
    return None
