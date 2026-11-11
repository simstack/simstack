"""Direct object transfers: API carries metadata; S3 carries file bytes."""
from __future__ import annotations

import hashlib
import http.client
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from simstack.util.file_hashing import hash_file
from simstack.util.file_transfer_client import (
    DownloadResult,
    FileTransferClient,
    FileTransferError,
)


def object_connection(url, timeout=60):
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
        raise FileTransferError("Server returned an invalid object storage URL")
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return cls(parsed.netloc, timeout=timeout), parsed.path + ("?" + parsed.query if parsed.query else "")


def upload_object(url, path, headers, timeout=60, offset=0, length=None):
    connection, request_path = object_connection(url, timeout)
    try:
        with Path(path).open("rb") as handle:
            size = Path(path).stat().st_size - offset if length is None else length
            handle.seek(offset)
            connection.putrequest("PUT", request_path)
            for key, value in {**headers, "Content-Length": str(size)}.items():
                connection.putheader(key, value)
            connection.endheaders()
            remaining = size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise FileTransferError("Upload source was truncated")
                connection.send(chunk)
                remaining -= len(chunk)
            response = connection.getresponse()
            response.read()
            if response.status >= 300:
                raise FileTransferError(f"Object upload returned HTTP {response.status}")
    finally:
        connection.close()


def download_object(metadata, target_path, timeout=60):
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    connection, request_path = object_connection(metadata["download_url"], timeout)
    temporary = None
    try:
        connection.request("GET", request_path)
        response = connection.getresponse()
        if response.status >= 300:
            response.read()
            raise FileTransferError(f"Object download returned HTTP {response.status}")
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=target_path.parent, prefix="." + target_path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        checksum = digest.hexdigest()
        if size != metadata["size_bytes"] or checksum != metadata["checksum_sha256"]:
            raise FileTransferError("Object download size or SHA256 checksum mismatch")
        if target_path.exists():
            raise FileExistsError(f"File {target_path} already exists.")
        temporary.replace(target_path)
        temporary = None
        return DownloadResult(path=target_path, size_bytes=size, checksum_sha256=checksum)
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ObjectFileClient(FileTransferClient):
    def __init__(self, *, object_api_prefix="/runner/objects", **kwargs):
        super().__init__(**kwargs)
        if object_api_prefix not in {
            "/runner/objects", "/files/objects", "/local-execution/v3/objects"
        }:
            raise ValueError("Unknown object transfer authorization")
        self.object_api_prefix = object_api_prefix

    @classmethod
    def from_context(cls):
        from simstack.core.context import context
        return cls.for_api(context.server.requests)

    @classmethod
    def for_api(cls, requests):
        return cls(server_url=requests.server_url, runner_token=requests.token,
                   account_id=requests.bootstrap_data["account_id"] if requests.bootstrap_data else None,
                   object_api_prefix=requests.object_api_prefix,
                   rotate_credentials=requests.object_api_prefix == "/runner/objects")

    def publish(self, file_stack, path):
        checksum = hash_file(path)
        payload = {
            "file_stack_id": str(file_stack.id), "name": file_stack.name,
            "size_bytes": Path(path).stat().st_size, "checksum_sha256": checksum,
            "is_hashable": file_stack.is_hashable,
            "multipart": Path(path).stat().st_size > 8 * 1024 * 1024,
        }
        staging_directory = getattr(file_stack, "_staging_directory", None)
        staged = staging_directory is not None and Path(path).resolve().is_relative_to(Path(staging_directory).resolve())
        if self.object_api_prefix == "/runner/objects" and not staged:
            payload["local_instance"] = file_stack.locations[0].model_dump(mode="json")
            if payload["local_instance"]["resource"] == {"value": "__pending__"}:
                from simstack.core.context import context
                from simstack.util.file_transfer_client import transfer_resource_name
                payload["local_instance"]["resource"] = {"value": transfer_resource_name(context.config.resource)}
        for attempt in range(2):
            prepared = self._json_request("POST", self.object_api_prefix + "/prepare", payload)
            try:
                if prepared.get("multipart"):
                    if self._publish_parts(prepared, path) != checksum:
                        raise FileTransferError("Source changed during multipart upload")
                else:
                    upload_object(prepared["upload_url"], path, prepared["upload_headers"], self.timeout_seconds)
                break
            except FileTransferError as exc:
                if attempt or "HTTP 403" not in str(exc):
                    raise
        completed = self._json_request("POST", f"{self.object_api_prefix}/{prepared['upload_id']}/complete", {},
            timeout_seconds=max(self.timeout_seconds, 60 + Path(path).stat().st_size // (1024 * 1024)))
        return next(location for location in completed["locations"] if location["location_type"] == "s3")

    def materialize_user_file(self, file_stack_id, target, *, object_key=None):
        from urllib.parse import urlencode
        if self.object_api_prefix == "/runner/objects":
            raise ValueError("Local materialization requires user or local execution authorization")
        query = "?" + urlencode({"object_key": object_key}) if object_key else ""
        metadata = self._json_request("GET", f"{self.object_api_prefix}/{file_stack_id}/download{query}")
        return download_object(metadata, target, self.timeout_seconds).path

    def copy_file(self, file_stack, source_file_id):
        source = next(location for location in file_stack.locations
                      if location.location_type == "s3" and location.status == "available")
        prepared = self._json_request("POST", self.object_api_prefix + "/prepare", {
            "file_stack_id": str(file_stack.id), "source_file_stack_id": str(source_file_id),
            "source_object_key": source.object_key,
            "name": file_stack.name, "size_bytes": source.size_bytes,
            "checksum_sha256": source.checksum_sha256, "is_hashable": file_stack.is_hashable,
        }, timeout_seconds=max(self.timeout_seconds, 60 + source.size_bytes // (1024 * 1024)))
        completed = self._json_request("POST", f"{self.object_api_prefix}/{prepared['upload_id']}/complete", {})
        return next(location for location in completed["locations"] if location["location_type"] == "s3")


    def _publish_parts(self, prepared, path):
        multipart = prepared["multipart"]
        full_digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for number in range(1, multipart["part_count"] + 1):
                offset = (number - 1) * multipart["part_size"]
                length = min(multipart["part_size"], Path(path).stat().st_size - offset)
                digest = hashlib.sha256()
                handle.seek(offset)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise FileTransferError("Upload source was truncated")
                    digest.update(chunk)
                    full_digest.update(chunk)
                    remaining -= len(chunk)
                payload = {"part_number": number, "size_bytes": length,
                           "checksum_sha256": digest.hexdigest()}
                for attempt in range(3):
                    signed = self._json_request("POST",
                        f"{self.object_api_prefix}/{prepared['upload_id']}/parts/prepare", payload)
                    try:
                        upload_object(signed["upload_url"], path, signed["upload_headers"],
                                      self.timeout_seconds, offset=offset, length=length)
                        break
                    except (FileTransferError, OSError):
                        if attempt == 2:
                            raise
        return full_digest.hexdigest()
