import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from simstack.core.node_runner import NodeRunner
from simstack.util.resource_config import ResourceConfig


@pytest.fixture(autouse=True)
def external_file_storage(tmp_path, monkeypatch):
    # These tests exercise process environments; log-file publication uses the
    # new storage boundary without making a network request.
    from datetime import datetime
    from simstack.models.file_instance import FileInstance
    from simstack.models.parameters import Resource
    from simstack.util.object_transfer import ObjectFileClient

    monkeypatch.setattr("simstack.core.context.context",
        SimpleNamespace(config=SimpleNamespace(workdir=tmp_path, resource=Resource(value="test"))))
    class Storage:
        def publish(self, value, path):
            return FileInstance(path="logs/" + str(value.id), resource="__s3__",
                location_type="s3", bucket="test", object_key="logs/" + str(value.id),
                created_at=datetime.now(), size_bytes=path.stat().st_size).model_dump()
    monkeypatch.setattr(ObjectFileClient, "from_context", lambda **kwargs: Storage())


@pytest.mark.parametrize("scratch", [False, True])
@pytest.mark.parametrize("real_runner", [False, True])
def test_run_env_forwarding(tmp_path, monkeypatch, scratch, real_runner):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PARNODES", "2")
    monkeypatch.setenv("PRESERVED_ENV", "original")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[test.setup]\n' + f'tmp_base_dir = "{(tmp_path / "scratch").as_posix()}"\n'
        '[test.program.turbomole]\n' + f'use_tmp = {str(scratch).lower()}\nuse_temp = {str(scratch).lower()}\n'
        'run_command = "echo test"\n'
    )
    config = ResourceConfig(config_file, "test")
    runner = NodeRunner("test", "test-task") if real_runner else None
    if runner and hasattr(runner, "_require_resource_config"):
        monkeypatch.setattr(runner, "_require_resource_config", lambda: config)
    launched = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", launched)
    config.run("turbomole", node_runner=runner, env={"PARNODES": "12"})
    process_env = launched.call_args.kwargs["env"]
    assert process_env["PARNODES"] == "12"
    assert process_env["PRESERVED_ENV"] == "original"
    assert os.environ["PARNODES"] == "2"
    if scratch:
        assert "scratch" in str(launched.call_args.kwargs["cwd"])


def test_custom_runner_receives_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text('[test.program.tm]\nrun_command = "echo test"\n')
    runner = SimpleNamespace(subprocess=Mock(return_value=True))
    ResourceConfig(path, "test").run("tm", node_runner=runner, env={"PARNODES": "12"})
    assert runner.subprocess.call_args.kwargs["env"] == {"PARNODES": "12"}


def test_real_subprocess_has_private_environment(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PARNODES", "2")
    runner = NodeRunner("test", "test-task")
    command = f'"{sys.executable}" -c "import os; print(os.environ[\'PARNODES\'])"'
    assert runner.subprocess("private-env", command, env={"PARNODES": "12"})
    assert runner.last_stdout.strip() == "12"
    assert os.environ["PARNODES"] == "2"
