"""Give the launching runner ownership of files a root container wrote."""

from __future__ import annotations

import os
from pathlib import Path

# Self-contained so `docker run --entrypoint python -c` can execute it inside
# an image that does not yet contain this module.
HARMONIZE_PY = r'''
import os
import sys


def harmonize(root, uid, gid):
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


if __name__ == "__main__":
    harmonize(sys.argv[3], int(sys.argv[1]), int(sys.argv[2]))
'''


def harmonize_output_tree(root: Path | str, uid: int | None, gid: int | None) -> None:
    namespace = {"__name__": "output_permissions_embed"}
    exec(HARMONIZE_PY, namespace)
    namespace["harmonize"](os.fspath(root), uid, gid)


def docker_harmonize_command(
    *,
    image: str,
    task_dir: Path | str,
    uid: int | None,
    gid: int | None,
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
    return [
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
        image,
        "-c",
        HARMONIZE_PY,
        str(uid),
        str(gid),
        "/work",
    ]
