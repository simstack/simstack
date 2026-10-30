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
    def publish(self, file_stack, path):
        checksum = hash_file(path)
        payload = {
            "file_stack_id": str(file_stack.id), "name": file_stack.name,
            "size_bytes": Path(path).stat().st_size, "checksum_sha256": checksum,
            "is_hashable": file_stack.is_hashable,
            "multipart": Path(path).stat().st_size > 8 * 1024 * 1024,
            "local_instance": file_stack.locations[0].model_dump(mode="json"),
        }
        for attempt in range(2):
            prepared = self._json_request("POST", "/runner/objects/prepare", payload)
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
        completed = self._json_request("POST", f"/runner/objects/{prepared['upload_id']}/complete", {})
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
                        f"/runner/objects/{prepared['upload_id']}/parts/prepare", payload)
                    try:
                        upload_object(signed["upload_url"], path, signed["upload_headers"],
                                      self.timeout_seconds, offset=offset, length=length)
                        break
                    except (FileTransferError, OSError):
                        if attempt == 2:
                            raise
        return full_digest.hexdigest()
