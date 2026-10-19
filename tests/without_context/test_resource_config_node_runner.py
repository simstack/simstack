"""Exercise the public ResourceConfig contract with real, bounded subprocesses."""

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from simstack.core.node_runner import NodeRunner
from simstack.models.parameters import Resource
from simstack.util.resource_config import ResourceConfig


@pytest.fixture
def task_runner(monkeypatch, tmp_path):
    workdir = tmp_path / "work"
    task_dir = workdir / "tasks" / "task-1"
    task_dir.mkdir(parents=True)
    monkeypatch.chdir(task_dir)
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(
            initialized=True,
            runner_mode=True,
            config=SimpleNamespace(workdir=workdir, max_inline_document_bytes=1_000_000,
                                   resource=Resource(value="local")),
        ),
    )
    return NodeRunner("program", "task-1")


def _config(tmp_path, content):
    config_path = tmp_path / "config.toml"
    config_path.write_text(content)
    return ResourceConfig(config_path, "resource")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax")
def test_string_scripts_preserve_redirects_variables_and_sequencing(
    monkeypatch, tmp_path, task_runner
):
    monkeypatch.setenv("OPERATOR_TOKEN", "must-not-leak")
    config = _config(tmp_path, """
[resource.setup]
scripts = ['''VALUE=setup; printf '%s' "$VALUE" > setup.txt''']
[resource.program.program]
run_command = '''VALUE=run; printf '%s' "$VALUE" > output.txt && printf done; test -z "$OPERATOR_TOKEN"'''
""")
    config.setup(node_runner=task_runner)
    config.run("program", node_runner=task_runner)

    assert (task_runner.task_workdir / "setup.txt").read_text() == "setup"
    assert (task_runner.task_workdir / "output.txt").read_text() == "run"
    assert task_runner.last_stdout == "done"
    assert task_runner.last_returncode == 0
    assert len(task_runner.info_files) == 2


def test_argv_command_does_not_interpret_shell_metacharacters(tmp_path, task_runner):
    command = [sys.executable, "-c", "import sys; print(sys.argv[1])", "a; $HOME > file"]
    config = _config(tmp_path, f"""
[resource.program.program]
run_command = {json.dumps(command)}
""")
    config.run("program", node_runner=task_runner)
    assert task_runner.last_stdout.strip() == "a; $HOME > file"
    assert not (task_runner.task_workdir / "file").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="Explicit Bash profile")
def test_configured_shell_preserves_source_semantics(tmp_path, task_runner):
    (task_runner.task_workdir / "environment.sh").write_text("VALUE=from-source\n")
    config = _config(tmp_path, """
[resource]
shell = ["bash", "-c"]
[resource.program.program]
run_command = '''source environment.sh && printf '%s' "$VALUE"'''
""")
    config.run("program", node_runner=task_runner)
    assert task_runner.last_stdout == "from-source"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax")
def test_real_runner_uses_task_owned_unique_scratch_and_copies_files(
    tmp_path, task_runner
):
    external = tmp_path / "operator-scratch"
    config = _config(tmp_path, f"""
[resource.setup]
tmp_base_dir = {json.dumps(str(external))}
[resource.program.program]
use_tmp = true
run_command = "pwd > where.txt; cat inputs/input.txt > output.txt"
input_files = ["inputs/input.txt"]
output_files = ["output.txt", "where.txt"]
scratch_cleanup = true
""")
    input_dir = task_runner.task_workdir / "inputs"
    input_dir.mkdir()
    (input_dir / "input.txt").write_text("input-data")
    config.run("program", node_runner=task_runner)
    first_cwd = (task_runner.task_workdir / "where.txt").read_text().strip()
    config.run("program", node_runner=task_runner)
    second_cwd = (task_runner.task_workdir / "where.txt").read_text().strip()

    assert first_cwd != second_cwd
    assert first_cwd.startswith(str(task_runner.task_workdir / "scratch") + "/")
    assert (task_runner.task_workdir / "output.txt").read_text() == "input-data"
    assert list((task_runner.task_workdir / "scratch").iterdir()) == []
    assert not external.exists()
    assert len(task_runner.info_files) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax")
@pytest.mark.parametrize("setup", [False, True])
def test_nonzero_exit_is_not_reported_as_resource_success(tmp_path, task_runner, setup):
    config = _config(tmp_path, """
[resource.setup]
scripts = ["printf failure >&2; exit 7"]
[resource.program.program]
use_temp = true
scratch_cleanup = true
run_command = "printf failure >&2; exit 7"
""")
    with pytest.raises(subprocess.CalledProcessError) as caught:
        if setup:
            config.setup(node_runner=task_runner)
        else:
            config.run("program", node_runner=task_runner)
    assert caught.value.returncode == 7
    assert caught.value.stderr == "failure"
    assert task_runner.last_returncode == 7
    assert len(task_runner.info_files) == 1
    if not setup:
        assert list((task_runner.task_workdir / "scratch").iterdir()) == []


@pytest.mark.parametrize("name", ["../outside.txt", "/outside.txt"])
def test_scratch_copy_rejects_paths_outside_task(tmp_path, task_runner, name):
    config = _config(tmp_path, f"""
[resource.program.program]
use_temp = true
scratch_cleanup = true
run_command = ["unused"]
input_files = [{json.dumps(name)}]
""")
    with pytest.raises(ValueError, match="relative to their task directory"):
        config.run("program", node_runner=task_runner)
    assert task_runner.info_files == []
    assert list((task_runner.task_workdir / "scratch").iterdir()) == []


def test_scratch_symlink_cannot_expand_runner_boundary(tmp_path, task_runner):
    external = tmp_path / "outside"
    external.mkdir()
    (external / "keep.txt").write_text("keep")
    (task_runner.task_workdir / "scratch").symlink_to(external, target_is_directory=True)
    config = _config(tmp_path, """
[resource.program.program]
use_temp = true
scratch_cleanup = true
run_command = ["unused"]
""")
    with pytest.raises(ValueError, match="scratch must stay"):
        config.run("program", node_runner=task_runner)
    assert (external / "keep.txt").read_text() == "keep"
    assert list(external.iterdir()) == [external / "keep.txt"]
