"""Tests for ResourceConfig.tmp_base_dir with plain paths and shell snippets."""

import tempfile
from pathlib import Path

from simstack.util.resource_config import ResourceConfig


def _make_config(tmp_path: Path, tmp_base_dir) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[test-resource]\n"
        "[test-resource.setup]\n"
        f'tmp_base_dir = """\n{tmp_base_dir}\n"""\n'
    )
    return cfg


def test_plain_path(tmp_path):
    base = tmp_path / "myscratch"
    rc = ResourceConfig(_make_config(tmp_path, base), "test-resource")
    assert rc.tmp_base_dir == base
    assert base.is_dir()


def test_plain_path_with_env_var(tmp_path, monkeypatch):
    target = tmp_path / "scratch2"
    monkeypatch.setenv("SIMSTACK_SCRATCH", str(target))
    rc = ResourceConfig(_make_config(tmp_path, "${SIMSTACK_SCRATCH}"), "test-resource")
    assert rc.tmp_base_dir == target
    assert target.is_dir()


def test_shell_snippet_resolves_scratch(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()  # snippet only selects $SCRATCH if it exists and is a dir (as on justus)
    monkeypatch.setenv("SCRATCH", str(scratch))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    snippet = (
        'if test -n "${SCRATCH}" -a -e "${SCRATCH}" -a -d "${SCRATCH}" '
        '-a "${SCRATCH}" != "/scratch" -a "${SCRATCH}" != "/tmp"; then\n'
        '  TMP_BASE_DIR="${SCRATCH}"\n'
        "else\n"
        '  TMP_BASE_DIR="${TMPDIR:-/tmp/${USER}}"\n'
        "fi\n"
    )
    rc = ResourceConfig(_make_config(tmp_path, snippet), "test-resource")
    assert rc.tmp_base_dir == scratch
    assert scratch.is_dir()


def test_shell_snippet_falls_back_to_tmpdir(tmp_path, monkeypatch):
    tmpdir = tmp_path / "tmpdir"
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    snippet = (
        'if test -n "${SCRATCH}" -a -d "${SCRATCH}"; then\n'
        '  TMP_BASE_DIR="${SCRATCH}"\n'
        "else\n"
        '  TMP_BASE_DIR="${TMPDIR:-/tmp}"\n'
        "fi\n"
    )
    rc = ResourceConfig(_make_config(tmp_path, snippet), "test-resource")
    assert rc.tmp_base_dir == tmpdir


def test_shell_snippet_single_line(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "testuser")
    tmpdir = tmp_path / "tmp"
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    rc = ResourceConfig(_make_config(tmp_path, 'TMP_BASE_DIR="${TMPDIR:-/tmp/${USER}}"'), "test-resource")
    assert rc.tmp_base_dir == tmpdir


def test_broken_snippet_falls_back_to_system_tempdir(tmp_path):
    rc = ResourceConfig(_make_config(tmp_path, "if [ ; then\n  echo oops\nfi"), "test-resource")
    assert rc.tmp_base_dir == Path(tempfile.gettempdir())


def test_missing_tmp_base_dir_falls_back_to_system_tempdir(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[test-resource]\n[test-resource.setup]\n")
    rc = ResourceConfig(cfg, "test-resource")
    assert rc.tmp_base_dir == Path(tempfile.gettempdir())
