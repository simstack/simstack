import pytest
from unittest.mock import MagicMock, patch, mock_open, call
import os
import subprocess
import sys
from pathlib import Path

from simstack.core.node_runner import NodeRunner
from simstack.core.simstack_result import SimstackResult
from simstack.core.definitions import TaskStatus
from simstack.models.files import FileStack


class TestNodeRunner:
    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    @pytest.fixture
    def node_runner(self, mock_logger):
        """Create a NodeRunner instance for testing"""
        return NodeRunner("test_node", "test_123", logger=mock_logger)

    def test_init_default_values(self, mock_logger):
        """Test NodeRunner initialization with default values"""
        runner = NodeRunner("node", "NA")

        # Test inherited SimstackResult properties
        assert hasattr(runner, "files")
        assert hasattr(runner, "info_files")
        assert hasattr(runner, "status")
        assert hasattr(runner, "message")
        assert hasattr(runner, "error_message")

        # Test NodeRunner specific properties
        assert runner.name == "node"
        assert runner.task_id == "NA"
        assert runner.logger is not None

    def test_init_custom_values(self, mock_logger):
        """Test NodeRunner initialization with custom values"""
        runner = NodeRunner(
            "custom_node", "custom_123", logger=mock_logger
        )

        assert runner.name == "custom_node"
        assert runner.task_id == "custom_123"
        assert runner.logger == mock_logger

    def test_debug_logging(self, node_runner, mock_logger):
        """Test debug logging method"""
        node_runner.debug("test debug message")

        mock_logger.debug.assert_called_once_with(
            "Task test_node: test debug message for task_id: test_123",
            stacklevel=2,
        )

    def test_info_logging(self, node_runner, mock_logger):
        """Test info logging method"""
        node_runner.info("test info message")

        # Should be called twice - once in __init__ and once in our test
        assert mock_logger.info.call_count == 2
        mock_logger.info.assert_called_with(
            "Task test_node: test info message task_id: test_123",
            stacklevel=2,
        )

    def test_warning_logging(self, node_runner, mock_logger):
        """Test warning logging method"""
        node_runner.warning("test warning message")

        mock_logger.warning.assert_called_once_with(
            "Task test_node: test warning message task_id: test_123",
            stacklevel=2,
        )

    def test_error_logging(self, node_runner, mock_logger):
        """Test error logging method"""
        node_runner.error("test error message")

        mock_logger.error.assert_called_once_with(
            "Task test_node: test error message task_id: test_123",
            stacklevel=2,
            exc_info=True,
        )

    @patch("subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch.object(FileStack, "from_local_file")
    def test_subprocess_success(
        self, mock_file_stack, mock_file, mock_subprocess, node_runner
    ):
        """Test successful subprocess execution"""
        # Mock subprocess return
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = "Success output"
        mock_process.stderr = ""
        mock_subprocess.return_value = mock_process

        # Mock FileStack creation
        mock_file_stack_instance = MagicMock()
        mock_file_stack.return_value = mock_file_stack_instance

        result = node_runner.subprocess("test_command", "echo 'hello'")

        assert result is True
        expected_kwargs = {
            "shell": True,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "cwd": None,
        }
        if os.name == "nt":
            expected_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        mock_subprocess.assert_called_once_with("echo 'hello'", **expected_kwargs)

        # Check that log file was created and FileStack was added
        mock_file.assert_called_with("test_command.log", "w", encoding="utf-8")
        mock_file_stack.assert_called_once_with(
            "test_command.log", in_memory=True, is_hashable=True, secure_source=True
        )
        assert mock_file_stack_instance in node_runner.info_files

    @patch("subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch.object(FileStack, "from_local_file")
    def test_subprocess_failure(
        self, mock_file_stack, mock_file, mock_subprocess, node_runner
    ):
        """Test failed subprocess execution"""
        # Mock subprocess return
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = ""
        mock_process.stderr = "Error occurred"
        mock_subprocess.return_value = mock_process

        # Mock FileStack creation
        mock_file_stack_instance = MagicMock()
        mock_file_stack.return_value = mock_file_stack_instance

        result = node_runner.subprocess("test_command", "false")

        assert result is False
        assert mock_file_stack_instance in node_runner.info_files

    @patch("subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    @patch.object(FileStack, "from_local_file")
    def test_subprocess_default_name(
        self, mock_file_stack, mock_file, mock_subprocess, node_runner
    ):
        """Test subprocess with empty name defaults to 'process'"""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process

        mock_file_stack_instance = MagicMock()
        mock_file_stack.return_value = mock_file_stack_instance

        node_runner.subprocess("", "echo 'test'")

        mock_file.assert_called_with("process.log", "w", encoding="utf-8")
        mock_file_stack.assert_called_once_with(
            "process.log", in_memory=True, is_hashable=True, secure_source=True
        )

    def test_fail_method(self, node_runner, mock_logger):
        """Test fail method sets correct status and error message"""
        error_msg = "Something went wrong"
        result = node_runner.fail(error_msg)

        # Check that it returns self (the NodeRunner instance)
        assert result is node_runner
        assert isinstance(result, SimstackResult)

        # Check that error was logged
        mock_logger.error.assert_called_with(
            "Task test_node: Something went wrong task_id: test_123",
            stacklevel=2,
            exc_info=True,
        )

        # Check that status and error_message were set
        assert node_runner.status == TaskStatus.FAILED
        assert node_runner.error_message == error_msg

    def test_succeed_method(self, node_runner, mock_logger):
        """Test succeed method sets correct status and message"""
        success_msg = "Task completed successfully"
        result = node_runner.succeed(success_msg)

        # Check that it returns self (the NodeRunner instance)
        assert result is node_runner
        assert isinstance(result, SimstackResult)

        # Check that success was logged
        expected_calls = [
            # First call from __init__
            call(
                "Task test_node: NodeRunner 'test_node' initialized for task_id: test_123 task_id: test_123",
                stacklevel=2,
            ),
            # Second call from succeed
            call(
                "Task test_node: succeeded Task completed successfully task_id: test_123",
                stacklevel=2,
            ),
        ]
        mock_logger.info.assert_has_calls(expected_calls)

        # Check that status and message were set
        assert node_runner.status == TaskStatus.COMPLETED
        assert node_runner.message == success_msg

    def test_succeed_method_without_message(self, node_runner, mock_logger):
        """Test succeed method with empty message"""
        result = node_runner.succeed()

        assert result is node_runner
        assert node_runner.status == TaskStatus.COMPLETED
        assert node_runner.message == ""

    def test_inherited_properties_from_simstack_result(self, node_runner):
        """Test that NodeRunner properly inherits from SimstackResult"""
        # Test that all SimstackResult properties are available
        assert hasattr(node_runner, "files")
        assert hasattr(node_runner, "info_files")
        assert hasattr(node_runner, "status")
        assert hasattr(node_runner, "message")
        assert hasattr(node_runner, "error_message")

        # Test that we can use inherited methods/properties
        assert isinstance(node_runner.files, list)
        assert isinstance(node_runner.info_files, list)

    def test_task_id_in_kwargs(self, mock_logger):
        """Test that task_id is properly handled"""
        runner = NodeRunner(
            "test", "specific_id", logger=mock_logger, other_param="value"
        )

        assert runner.task_id == "specific_id"
        assert runner.name == "test"


class TestNodeRunnerIntegration:
    """Integration tests for NodeRunner with real file operations"""

    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    def test_full_workflow_success(self, mock_logger, tmp_path):
        """Test a complete successful workflow with real file operations"""
        # Change to temp directory for test
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            runner = NodeRunner(
                "integration_test", "int_123", logger=mock_logger
            )

            # Test subprocess with real command
            result = runner.subprocess("test_echo", "echo 'Hello World'")

            assert result is True

            # Check that log file was actually created
            log_file = tmp_path / "test_echo.log"
            assert log_file.exists()

            # Check log file contents
            log_content = log_file.read_text()
            assert "Command: test_echo" in log_content
            assert "echo 'Hello World'" in log_content
            assert "Hello World" in log_content

            # Test success method
            final_result = runner.succeed("Integration test completed")

            assert final_result is runner
            assert runner.status == TaskStatus.COMPLETED
            assert runner.message == "Integration test completed"
            assert len(runner.info_files) == 1  # Should have the log file

        finally:
            os.chdir(original_cwd)

    def test_full_workflow_failure(self, mock_logger, tmp_path):
        """Test a complete failed workflow"""
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            runner = NodeRunner(
                "integration_fail", "fail_123", logger=mock_logger
            )

            # Test subprocess with failing command
            result = runner.subprocess("test_fail", "exit 1")

            assert result is False

            # Check that log file was created even for failed command
            log_file = tmp_path / "test_fail.log"
            assert log_file.exists()

            # Test fail method
            final_result = runner.fail("Integration test failed")

            assert final_result is runner
            assert runner.status == TaskStatus.FAILED
            assert runner.error_message == "Integration test failed"
            assert len(runner.info_files) == 1  # Should have the log file

        finally:
            os.chdir(original_cwd)


def _write_runner_config(tmp_path, extra_program_lines="", scratch_cleanup=None, tmp_base_dir=True):
    from simstack.util.resource_config import ResourceConfig

    scratch_base = tmp_path / "scratch_base"
    config_file = tmp_path / "config.toml"
    lines = []
    if tmp_base_dir:
        lines.append("[self.setup]")
        lines.append(f'tmp_base_dir = "{scratch_base.as_posix()}"')
    lines.append("[self.program.orca]")
    script = tmp_path / "write_out.py"
    script.write_text("open('out.txt', 'w', encoding='utf-8').write('from-scratch')\n")
    exe = sys.executable.replace("\\", "/")
    script_posix = script.as_posix()
    lines.append(f'run_command = "\\"{exe}\\" \\"{script_posix}\\""')
    if scratch_cleanup is True:
        lines.append("scratch_cleanup = true")
    elif scratch_cleanup is False:
        lines.append("scratch_cleanup = false")
    if extra_program_lines:
        lines.append(extra_program_lines)
    config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ResourceConfig(config_file, "self"), scratch_base


class TestNodeRunnerStageExecuteRetrieve:
    def test_stage_creates_scratch_and_copies_inputs(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "in.txt").write_text("input-data", encoding="utf-8")
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            fs = MagicMock()
            scratch = runner.stage(input_files=["in.txt", fs])
            assert scratch.resolve() == (scratch_base / "task_abc").resolve()
            assert (scratch / "in.txt").read_text(encoding="utf-8") == "input-data"
            fs.get.assert_called_once_with(local_dir=scratch)
            assert Path.cwd().resolve() == workdir.resolve()
        finally:
            os.chdir(original_cwd)

    def test_stage_missing_input_raises(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            with pytest.raises(ValueError, match="does not exist"):
                runner.stage(input_files=["missing.txt"])
        finally:
            os.chdir(original_cwd)

    def test_stage_without_tmp_base_dir_raises(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        monkeypatch.delenv("TMP_BASE_DIR", raising=False)
        rc, _scratch_base = _write_runner_config(tmp_path, tmp_base_dir=False)
        monkeypatch.setattr(context, "_resource_config", rc)
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="tmp_base_dir"):
            runner.stage()

    def test_stage_without_resource_config_raises(self, monkeypatch):
        from simstack.core.context import context

        monkeypatch.setattr(context, "_resource_config", None)
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="resource_config"):
            runner.stage()

    def test_stage_context_not_initialized_raises(self, monkeypatch):
        from simstack.core.context import context

        monkeypatch.setattr(context, "_initialized", False)
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="not initialized"):
            runner.stage()

    def test_stage_twice_raises(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        runner = NodeRunner("orca", "task_abc")
        runner.stage()
        with pytest.raises(ValueError, match="already staged"):
            runner.stage()

    def test_execute_runs_in_scratch(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            scratch = runner.stage()
            assert runner.execute("orca") is True
            assert (scratch / "out.txt").read_text(encoding="utf-8") == "from-scratch"
            assert not (workdir / "out.txt").exists()
            assert (scratch / "orca.log").exists()
        finally:
            os.chdir(original_cwd)

    def test_execute_missing_program_raises(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="missing_prog"):
            runner.execute("missing_prog")

    def test_execute_missing_run_command_raises(self, tmp_path, monkeypatch):
        from simstack.core.context import context
        from simstack.util.resource_config import ResourceConfig

        config_file = tmp_path / "config.toml"
        config_file.write_text(
            f"""
[self.setup]
tmp_base_dir = "{(tmp_path / "scratch_base").as_posix()}"
[self.program.orca]
use_temp = true
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(context, "_resource_config", ResourceConfig(config_file, "self"))
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="run_command"):
            runner.execute("orca")

    def test_retrieve_copies_listed_files(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            scratch = runner.stage()
            (scratch / "keep.txt").write_text("keep", encoding="utf-8")
            (scratch / "skip.txt").write_text("skip", encoding="utf-8")
            runner.retrieve(output_files=["keep.txt"])
            assert (workdir / "keep.txt").read_text(encoding="utf-8") == "keep"
            assert not (workdir / "skip.txt").exists()
            assert scratch.exists()
            assert runner.scratch_dir is None
        finally:
            os.chdir(original_cwd)

    def test_retrieve_copies_all_when_omitted(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            scratch = runner.stage()
            (scratch / "a.txt").write_text("a", encoding="utf-8")
            (scratch / "b.txt").write_text("b", encoding="utf-8")
            runner.retrieve()
            assert (workdir / "a.txt").read_text(encoding="utf-8") == "a"
            assert (workdir / "b.txt").read_text(encoding="utf-8") == "b"
        finally:
            os.chdir(original_cwd)

    def test_retrieve_honors_scratch_cleanup(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, _scratch_base = _write_runner_config(tmp_path, scratch_cleanup=True)
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_abc")
            scratch = runner.stage()
            runner.execute("orca")
            runner.retrieve()
            assert (workdir / "out.txt").read_text(encoding="utf-8") == "from-scratch"
            assert not scratch.exists()
        finally:
            os.chdir(original_cwd)

    def test_retrieve_without_stage_raises(self):
        runner = NodeRunner("orca", "task_abc")
        with pytest.raises(ValueError, match="stage"):
            runner.retrieve()

    def test_resource_config_run_delegates_to_node_runner(self, tmp_path, monkeypatch):
        from simstack.core.context import context

        rc, scratch_base = _write_runner_config(
            tmp_path,
            extra_program_lines='use_temp = true\noutput_files = ["out.txt"]',
        )
        monkeypatch.setattr(context, "_resource_config", rc)
        workdir = tmp_path / "work"
        workdir.mkdir()
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            runner = NodeRunner("orca", "task_run")
            rc.run("orca", node_runner=runner)
            assert (workdir / "out.txt").read_text(encoding="utf-8") == "from-scratch"
            assert (scratch_base / "task_run").exists()
        finally:
            os.chdir(original_cwd)


class TestNodeRunnerScratch:
    def test_enter_scratch_logs_chdir_and_copies_back(self, tmp_path):
        workdir = tmp_path / "work"
        scratch = tmp_path / "scratch"
        workdir.mkdir()
        original_cwd = os.getcwd()
        saved_env = {key: os.environ.get(key) for key in ("TURBOTMPDIR", "TMPDIR", "TMP")}
        os.chdir(workdir)
        mock_logger = MagicMock()
        try:
            runner = NodeRunner("turbomole2", "task_abc", logger=mock_logger)
            result = runner.enter_scratch(
                scratch_dir=scratch, chdir=True, scratch_cleanup=False
            )
            assert result.resolve() == scratch.resolve()
            assert Path.cwd().resolve() == scratch.resolve()
            assert os.environ.get("TURBOTMPDIR") == str(scratch)
            (scratch / "tmp.dat").write_text("scratch-data", encoding="utf-8")
            mock_logger.info.assert_any_call(
                "Task turbomole2: Writing temporary files to scratch directory: "
                f"{scratch} task_id: task_abc",
                stacklevel=2,
            )
            runner.leave_scratch()
            assert Path.cwd().resolve() == workdir.resolve()
            assert (workdir / "tmp.dat").read_text(encoding="utf-8") == "scratch-data"
            assert scratch.exists()
        finally:
            os.chdir(original_cwd)
            for key, previous in saved_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def test_enter_scratch_cleanup_removes_directory(self, tmp_path):
        workdir = tmp_path / "work"
        scratch = tmp_path / "scratch"
        workdir.mkdir()
        original_cwd = os.getcwd()
        saved_env = {key: os.environ.get(key) for key in ("TURBOTMPDIR", "TMPDIR", "TMP")}
        os.chdir(workdir)
        try:
            runner = NodeRunner("turbomole2", "task_abc")
            runner.enter_scratch(scratch_dir=scratch, chdir=True, scratch_cleanup=True)
            (scratch / "gone.txt").write_text("x", encoding="utf-8")
            runner.leave_scratch()
            assert not scratch.exists()
            assert (workdir / "gone.txt").read_text(encoding="utf-8") == "x"
        finally:
            os.chdir(original_cwd)
            for key, previous in saved_env.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def test_enter_scratch_without_context_returns_none(self):
        runner = NodeRunner("turbomole2", "task_abc")
        assert runner.enter_scratch("turbomole2") is None

    def test_subprocess_logs_when_cwd_is_scratch(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        mock_logger = MagicMock()
        runner = NodeRunner("orca", "task_abc", logger=mock_logger)
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.subprocess("run", "echo scratch-run", cwd=str(scratch))
            assert result is True
            assert (scratch / "run.log").exists()
            mock_logger.info.assert_any_call(
                "Task orca: Writing temporary files to scratch directory: "
                f"{scratch} task_id: task_abc",
                stacklevel=2,
            )
        finally:
            os.chdir(original_cwd)
