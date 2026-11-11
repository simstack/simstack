from pathlib import Path

import pytest

from simstack.runner_runtime.project_protocol import verify_workflow_protocol


@pytest.mark.parametrize("content", [None, "[project]\nname='old'\n", "[tool.simstack]\nrunner-protocol=2\n",
    "[tool.simstack]\nrunner-protocol=true\n", "[tool.simstack]\nrunner-protocol='3'\n"])
def test_incompatible_or_undeclared_workflow_is_rejected_before_loading(tmp_path: Path, content):
    if content is not None:
        (tmp_path / "pyproject.toml").write_text(content)
    with pytest.raises(ValueError, match="runner-protocol"):
        verify_workflow_protocol(tmp_path)


def test_plain_python_scripts_can_declare_protocol_without_a_package_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.simstack]\nrunner-protocol=3\n")
    manifest = verify_workflow_protocol(tmp_path)
    assert manifest["tool"]["simstack"]["runner-protocol"] == 3
    assert "project" not in manifest


def test_package_manifest_keeps_existing_dependency_and_extras_configuration(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='science'\nversion='1.0'\n[tool.simstack]\nrunner-protocol=3\n")
    assert verify_workflow_protocol(tmp_path)["project"]["name"] == "science"
