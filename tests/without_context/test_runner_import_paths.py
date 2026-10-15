from __future__ import annotations

import subprocess
import sys


def test_worker_import_roots_are_explicit_and_independent_of_cwd(tmp_path):
    project = tmp_path / "project"
    declared = project / "workflows"
    declared.mkdir(parents=True)
    (project / "root_workflow.py").write_text("VALUE = 'root'\n")
    (declared / "declared_workflow.py").write_text("VALUE = 'declared'\n")
    outside = tmp_path / "task"
    outside.mkdir()
    script = """
import sys
from pathlib import Path
from simstack.runner_runtime.config import RunnerConfig
from simstack.runner_runtime.import_paths import configure_runner_import_paths
config = RunnerConfig({'python_paths': ['workflows']}, Path(sys.argv[1]), 'http://localhost')
configure_runner_import_paths(config)
import root_workflow, declared_workflow
assert root_workflow.VALUE == 'root'
assert declared_workflow.VALUE == 'declared'
assert str(Path(sys.argv[1]) / 'src') not in sys.path
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(project)], cwd=outside,
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_declared_roots_cannot_expose_database_runtime(tmp_path):
    (tmp_path / "pymongo.py").write_text("# forbidden driver exposed by workflow roots\n")
    script = """
import sys
from pathlib import Path
from simstack.runner_runtime.config import RunnerConfig
from simstack.runner_runtime.import_paths import configure_runner_import_paths
from simstack.runner_runtime.environment import assert_database_runtime_unavailable
configure_runner_import_paths(RunnerConfig({'python_paths': []}, Path(sys.argv[1]), 'http://localhost'))
try:
    assert_database_runtime_unavailable()
except RuntimeError as exc:
    assert 'pymongo' in str(exc)
else:
    raise AssertionError('Configured root exposed a database driver')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
