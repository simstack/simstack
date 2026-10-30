"""Reload resource credentials without restarting running workflow processes."""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def credential_lock(handle, *, exclusive=False):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
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
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
        raise ValueError("Runner credentials file has no token")
    return data


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
CONTAINER_CREDENTIAL_TMPFS = "/run/simstack-auth:rw,noexec,nosuid,nodev,size=1048576,mode=1733"
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
                ["docker", "exec", "-i", container_id, interpreter, "-c", CONTAINER_CREDENTIAL_WRITER],
                input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
    return False
