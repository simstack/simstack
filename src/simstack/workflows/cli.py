"""HTTP client for the same workflow installation API used by the UI."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .archive import snapshot_folder, validate_archive


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _installation_failure(result: dict) -> str | None:
    if result.get("status") == "failed":
        detail = result.get("error") or "Workflow package inspection failed"
        return f"{detail}. Correct the package and upload again."
    failed = [
        f"{target['resource']}: {target.get('error') or 'Runner preparation failed'}"
        for target in result.get("resources", [])
        if target.get("status") == "failed"
    ]
    if failed:
        return (
            "Workflow resource preparation failed: "
            + "; ".join(failed)
            + ". Correct the resource configuration or environment, then retry "
            + f"confirmation for installation {result.get('id', '<unknown>')}."
        )
    return None


class InstallationClient:
    def __init__(self, server_url: str, token: str, *, timeout: float = 60):
        parsed = urlparse(server_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Server URL must be an HTTP(S) URL without query or fragment"
            )
        if not token or token != token.strip():
            raise ValueError("An authenticated user token is required")
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self.base = server_url.rstrip("/")
        self.token, self.timeout = token, timeout
        self._opener = build_opener(_NoRedirect)

    def request(self, method, path, *, body=None, content_type=None):
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token,
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Workflow API returned HTTP {exc.code}: {detail}"
            ) from None
        except URLError as exc:
            raise RuntimeError(
                f"Workflow API connection failed: {exc.reason}"
            ) from None

    def upload(self, source: Path, resources: list[str]):
        data = snapshot_folder(source) if source.is_dir() else source.read_bytes()
        validate_archive(data)
        boundary = "simstack-" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="resources"\r\n\r\n'.encode()
            + json.dumps(resources).encode()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="workflow.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.request(
            "POST",
            "/workflow-installations",
            body=body,
            content_type="multipart/form-data; boundary=" + boundary,
        )

    def status(self, installation_id: str):
        return self.request(
            "GET", "/workflow-installations/" + quote(installation_id, safe="")
        )

    def confirm(self, installation_id: str):
        return self.request(
            "POST",
            "/workflow-installations/" + quote(installation_id, safe="") + "/confirm",
            body=b"{}",
            content_type="application/json",
        )

    def wait(self, installation_id: str, *, timeout: float, interval: float):
        if timeout <= 0 or interval <= 0:
            raise ValueError("Wait timeout and interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            result = self.status(installation_id)
            if result.get("status") in {"staged", "active"} or _installation_failure(
                result
            ):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Workflow installation did not reach a reviewable state in time"
                )
            time.sleep(min(interval, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.environ.get("SIMSTACK_SERVER_URL"))
    parser.add_argument(
        "--token",
        default=os.environ.get("SIMSTACK_USER_TOKEN"),
        help="User bearer token; prefer SIMSTACK_USER_TOKEN to avoid shell history",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="Read an existing token file; this command never saves credentials",
    )
    parser.add_argument("--http-timeout", type=float, default=60)
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser(
        "upload", help="Upload ZIP or a deterministic folder snapshot for preview"
    )
    upload.add_argument("source", type=Path)
    upload.add_argument(
        "--resource",
        action="append",
        required=True,
        help="Target resource; may be repeated",
    )
    upload.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the preview, or resource readiness when used with --confirm",
    )
    upload.add_argument(
        "--confirm",
        action="store_true",
        help="Explicitly activate after successful preview",
    )
    commands.add_parser("list")
    for name in ("status", "confirm", "wait"):
        command = commands.add_parser(name)
        command.add_argument("installation_id")
    parser.add_argument("--wait-timeout", type=float, default=600)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    try:
        token = args.token_file.read_text().strip() if args.token_file else args.token
        client = InstallationClient(
            args.server_url or "", token or "", timeout=args.http_timeout
        )
        if args.command == "upload":
            result = client.upload(args.source, args.resource)
            identifier = result["id"]
            if args.wait or args.confirm:
                result = client.wait(
                    identifier, timeout=args.wait_timeout, interval=args.poll_interval
                )
            if args.confirm:
                failure = _installation_failure(result)
                if failure:
                    raise RuntimeError(failure)
                result = client.confirm(identifier)
                if args.wait:
                    result = client.wait(
                        identifier,
                        timeout=args.wait_timeout,
                        interval=args.poll_interval,
                    )
        elif args.command == "list":
            result = client.request("GET", "/workflow-installations")
        elif args.command == "status":
            result = client.status(args.installation_id)
        elif args.command == "confirm":
            result = client.confirm(args.installation_id)
        else:
            result = client.wait(
                args.installation_id,
                timeout=args.wait_timeout,
                interval=args.poll_interval,
            )
        if args.command not in {"status", "list"}:
            failure = _installation_failure(result)
            if failure:
                raise RuntimeError(failure)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
