"""Process-lifetime cache pins without one open descriptor per input file."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from simstack.runner_runtime.launch_claims import _reserve_path

_owners = {}
_guard = threading.Lock()


def _pin_directory(path):
    # Adjacent markers continue identifying the same file on shared filesystems
    # even when a compute host mounts its parent at a different absolute path.
    key = hashlib.sha256(path.name.encode()).hexdigest()
    directory = path.parent / ".simstack-cache-pins" / key
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def _file_lock(directory, *, wait):
    handle = _reserve_path(directory / "lock")
    while handle is None and wait:
        time.sleep(0.01)
        handle = _reserve_path(directory / "lock")
    try:
        yield handle
    finally:
        if handle is not None:
            handle.close()


def _owner(workdir):
    key = (os.getpid(), str(workdir))
    with _guard:
        previous = _owners.get(key)
        if previous is not None:
            return previous[0]
        directory = workdir / ".cache-pins" / "owners"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / uuid4().hex
        handle = _reserve_path(path)
        if handle is None:
            raise RuntimeError("Could not create cache owner lease")
        _owners[key] = (path, handle)
        return path


def pin_cached_file(path: Path, *, workdir=None, allow_missing=False) -> bool:
    """Pin a returned path until its process exits, including abrupt death.

    A single held lease belongs to each process/workdir. Per-file markers point
    to that lease; eviction probes the lease rather than trusting a PID on a
    potentially different host. Short file locks serialize marker publication
    with unlink without retaining a descriptor for every scientific input.
    Downloads reserve an absent target before publishing bytes. A failed
    download leaves this harmless reservation until its process exits.
    """
    path = path.resolve()
    root = Path(workdir or os.environ.get("SIMSTACK_RUNNER_WORKDIR") or path.parent).resolve()
    directory = _pin_directory(path)
    with _file_lock(directory, wait=True):
        if not path.is_file() and (not allow_missing or path.exists()):
            return False
        owner = _owner(root)
        marker = directory / (owner.name + ".pin")
        if not marker.exists():
            temporary = directory / (owner.name + ".tmp")
            temporary.write_text(os.path.relpath(owner, directory))
            temporary.replace(marker)
        return True


def evict_cached_file(path: Path) -> bool:
    """Delete only after every published owner lease has been released."""
    path = path.resolve()
    directory = _pin_directory(path)
    with _file_lock(directory, wait=False) as guard:
        if guard is None:
            return False
        for marker in directory.glob("*.pin"):
            owner = directory / marker.read_text()
            # Missing/malformed ownership evidence does not prove termination.
            if not owner.is_file():
                return False
            lease = _reserve_path(owner)
            if lease is None:
                return False
            lease.close()
            marker.unlink()
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            return False
        return True


@atexit.register
def _release_pins():
    for _, handle in _owners.values():
        handle.close()
    _owners.clear()
