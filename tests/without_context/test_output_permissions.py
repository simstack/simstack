import os
import stat
from pathlib import Path

import pytest

from simstack.util.output_permissions import (
    docker_harmonize_command,
    harmonize_output_tree,
)


class _Stat:
    def __init__(self, mode: int):
        self.st_mode = mode


def _record_ownership(monkeypatch):
    chowns = []
    lchowns = []
    chmods = []

    def chown(path, uid, gid):
        chowns.append((os.fspath(path), uid, gid))

    def lchown(path, uid, gid):
        lchowns.append((os.fspath(path), uid, gid))

    def chmod(path, mode):
        chmods.append((os.fspath(path), mode))

    monkeypatch.setattr(os, "chown", chown, raising=False)
    monkeypatch.setattr(os, "lchown", lchown, raising=False)
    monkeypatch.setattr(os, "chmod", chmod)
    return chowns, lchowns, chmods


def test_harmonize_sets_owner_and_modes(tmp_path: Path, monkeypatch):
    task = tmp_path / "task"
    nested = task / "partial"
    nested.mkdir(parents=True)
    plain = nested / "result.wfn.npy"
    plain.write_bytes(b"npy")
    script = task / "script.sh"
    script.write_text("#!/bin/sh\n")
    link_name = task / "alias.npy"
    link_name.write_bytes(b"not-a-real-link")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    real_lstat = os.lstat
    real_islink = os.path.islink

    def lstat(path):
        text = os.fspath(path)
        if text.endswith("result.wfn.npy"):
            return _Stat(stat.S_IFREG | 0o600)
        if text.endswith("script.sh"):
            return _Stat(stat.S_IFREG | 0o755)
        return real_lstat(path)

    def islink(path):
        return os.fspath(path).endswith("alias.npy") or real_islink(path)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "islink", islink)
    chowns, lchowns, chmods = _record_ownership(monkeypatch)

    harmonize_output_tree(task, 1000, 1000)

    chown_paths = {path for path, _uid, _gid in chowns}
    assert os.fspath(task) in chown_paths
    assert os.fspath(nested) in chown_paths
    assert os.fspath(plain) in chown_paths
    assert os.fspath(script) in chown_paths
    assert os.fspath(link_name) not in chown_paths
    assert os.fspath(outside) not in chown_paths
    assert all(uid == 1000 and gid == 1000 for _path, uid, gid in chowns)
    assert (os.fspath(link_name), 1000, 1000) in lchowns
    assert os.fspath(outside) not in {path for path, _uid, _gid in lchowns}

    modes = dict(chmods)
    assert modes[os.fspath(task)] == 0o775
    assert modes[os.fspath(nested)] == 0o775
    assert modes[os.fspath(plain)] == 0o664
    assert modes[os.fspath(script)] == 0o775
    assert os.fspath(link_name) not in modes
    assert os.fspath(outside) not in modes


@pytest.mark.parametrize(
    ("uid", "gid"),
    [(None, 1000), (1000, None), (True, 1000), (1000, True), (-1, 1000)],
)
def test_harmonize_rejects_missing_owner(tmp_path: Path, uid, gid):
    task = tmp_path / "task"
    task.mkdir()
    with pytest.raises(ValueError):
        harmonize_output_tree(task, uid, gid)


def test_docker_harmonize_command_targets_only_the_task_dir(tmp_path: Path):
    task = tmp_path / "psi4_calculator" / "abc"
    task.mkdir(parents=True)
    command = docker_harmonize_command(
        image="molecular-qm-psi4:latest",
        task_dir=task,
        uid=1000,
        gid=1000,
    )
    assert f"{task.resolve()}:/work" in command
    assert f"{tmp_path.resolve()}:/work" not in command
    assert command[-3:] == ["1000", "1000", "/work"]


def test_docker_harmonize_command_rejects_missing_owner(tmp_path: Path):
    with pytest.raises(ValueError):
        docker_harmonize_command(
            image="molecular-qm-psi4:latest",
            task_dir=tmp_path,
            uid=None,
            gid=1000,
        )
