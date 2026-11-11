"""Reload resource credentials without restarting running workflow processes."""

import errno
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def credential_lock(handle, *, exclusive=False, blocking=True):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(
                handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1
            )
        except OSError as exc:
            if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError(exc.errno, "File is already locked") from exc
            raise
    else:
        import fcntl

        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(
            handle.fileno(), operation if blocking else operation | fcntl.LOCK_NB
        )
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_credentials(path):
    with Path(path).open(encoding="utf-8") as handle, credential_lock(handle):
        data = json.load(handle)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("token"), str)
        or not data["token"]
    ):
        raise ValueError("Runner credentials file has no token")
    return data


def configure_git_credentials(credentials):
    """Restore installer-owned SSH file paths when the agent is restarted."""
    for field, variable in (
        ("git_ssh_key", "SIMSTACK_GIT_SSH_KEY"),
        ("git_known_hosts", "SIMSTACK_GIT_KNOWN_HOSTS"),
    ):
        value = credentials.get(field)
        if value:
            os.environ[variable] = str(Path(value).expanduser().resolve())
    if credentials.get("git_ssh_key_version") is not None:
        os.environ["SIMSTACK_GIT_SSH_KEY_VERSION"] = str(
            credentials["git_ssh_key_version"]
        )


def update_known_hosts(value):
    """Install only the host keys explicitly trusted in the owner's Server UI."""
    path = os.environ.get("SIMSTACK_GIT_KNOWN_HOSTS")
    if path is None or value is None:
        return
    if not isinstance(value, str):
        raise ValueError("Invalid trusted Git host key response")
    target = Path(path)
    if target.is_file() and target.read_text() == value:
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".known-hosts-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def current_token(server_url, fallback):
    path = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
    if not path:
        return fallback
    try:
        data = read_credentials(path)
    except (FileNotFoundError, PermissionError):
        # Docker starts with its token environment; its configured user receives
        # a private credential copy on the persistent agent's next control poll.
        return fallback
    if data.get("server_url", "").rstrip("/") != server_url.rstrip("/"):
        raise ValueError("Runner credentials belong to a different server")
    return data["token"]


def refreshed_token(server_url, previous, *, timeout=10):
    if not os.environ.get("SIMSTACK_RUNNER_CREDENTIALS"):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = current_token(server_url, previous)
        if token != previous:
            return token
        time.sleep(0.1)
    return None


# This tmpfs is outside source/workflow data, so collectors never archive a
# resource token. Its configured image user owns the private 0600 file.
CONTAINER_CREDENTIAL_PATH = "/run/simstack-auth/credentials.json"
CONTAINER_CREDENTIAL_TMPFS = (
    "/run/simstack-auth:rw,noexec,nosuid,nodev,size=1048576,mode=1733"
)
CONTAINER_CREDENTIAL_WRITER = """
import os, sys, tempfile
path = os.environ["SIMSTACK_RUNNER_CREDENTIALS"]
fd, temporary = tempfile.mkstemp(prefix="credentials-", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(sys.stdin.buffer.read())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""


def copy_container_credentials(container_id, credentials):
    import subprocess

    payload = json.dumps(credentials).encode()
    for interpreter in ("python3", "python"):
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container_id,
                    interpreter,
                    "-c",
                    CONTAINER_CREDENTIAL_WRITER,
                ],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
    return False
