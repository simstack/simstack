"""Give the launching runner ownership of files a root container wrote."""

from __future__ import annotations

import os
from pathlib import Path

# Self-contained so `docker run --entrypoint python -c` can execute it inside
# an image that does not yet contain this module.
HARMONIZE_PY = r'''
import os
import sys


def harmonize(root, uid, gid, parent=None):
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ValueError(
            f"output owner uid must be a non-negative integer, got {uid!r}"
        )
    if isinstance(gid, bool) or not isinstance(gid, int) or gid < 0:
        raise ValueError(
            f"output owner gid must be a non-negative integer, got {gid!r}"
        )
    if not os.path.isdir(root):
        raise ValueError(f"task directory does not exist: {root}")

    def apply_dir(path):
        if os.path.islink(path):
            os.lchown(path, uid, gid)
            return
        os.chown(path, uid, gid)
        os.chmod(path, 0o775)

    apply_dir(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if os.path.abspath(dirpath) != os.path.abspath(root):
            apply_dir(dirpath)
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                os.lchown(path, uid, gid)
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                os.lchown(path, uid, gid)
                continue
            os.chown(path, uid, gid)
            mode = os.lstat(path).st_mode
            os.chmod(path, 0o775 if mode & 0o111 else 0o664)

    if parent is None:
        return
    if not isinstance(parent, str) or not parent:
        raise ValueError(f"parent directory must be a path, got {parent!r}")
    if os.path.abspath(parent) == os.path.abspath(root):
        raise ValueError("parent directory must not be the task directory")
    if not os.path.isdir(parent):
        raise ValueError(f"parent directory does not exist: {parent}")
    apply_dir(parent)


if __name__ == "__main__":
    parent = sys.argv[4] if len(sys.argv) > 4 else None
    harmonize(sys.argv[3], int(sys.argv[1]), int(sys.argv[2]), parent)
'''


def output_owner_ids(path: Path | str) -> tuple[int, int]:
    """Uid and gid that should own task output: the workdir owner, never root."""
    try:
        stat_result = os.stat(path)
    except OSError as exc:
        raise ValueError(f"cannot stat workdir {path}: {exc}") from exc
    uid = getattr(stat_result, "st_uid", None)
    gid = getattr(stat_result, "st_gid", None)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ValueError(
            f"output owner uid must be a non-negative integer, got {uid!r}"
        )
    if isinstance(gid, bool) or not isinstance(gid, int) or gid < 0:
        raise ValueError(
            f"output owner gid must be a non-negative integer, got {gid!r}"
        )
    if uid == 0 or gid == 0:
        raise ValueError(f"refusing to harmonize task output to root ({uid}:{gid})")
    return uid, gid


def harmonize_output_tree(
    root: Path | str,
    uid: int | None,
    gid: int | None,
    parent: Path | str | None = None,
) -> None:
    namespace = {"__name__": "output_permissions_embed"}
    exec(HARMONIZE_PY, namespace)
    namespace["harmonize"](
        os.fspath(root),
        uid,
        gid,
        None if parent is None else os.fspath(parent),
    )


def docker_harmonize_command(
    *,
    image: str,
    task_dir: Path | str,
    uid: int | None,
    gid: int | None,
    parent_dir: Path | str | None = None,
) -> list[str]:
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ValueError(
            f"output owner uid must be a non-negative integer, got {uid!r}"
        )
    if isinstance(gid, bool) or not isinstance(gid, int) or gid < 0:
        raise ValueError(
            f"output owner gid must be a non-negative integer, got {gid!r}"
        )
    if not image:
        raise ValueError("docker image is required to harmonize task output")
    host_dir = Path(task_dir).resolve()
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        "python",
        "-v",
        f"{host_dir}:/work",
    ]
    script_args = [str(uid), str(gid), "/work"]
    if parent_dir is not None:
        parent = Path(parent_dir).resolve()
        if parent == host_dir:
            raise ValueError("parent directory must not be the task directory")
        command.extend(["-v", f"{parent}:/parent"])
        script_args.append("/parent")
    command.extend([image, "-c", HARMONIZE_PY, *script_args])
    return command
