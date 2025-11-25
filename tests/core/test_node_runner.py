import pytest
from unittest.mock import MagicMock, patch, mock_open, call
import os

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
        return NodeRunner(name="test_node", logger=mock_logger, task_id="test_123")

    def test_init_default_values(self, mock_logger):
        """Test NodeRunner initialization with default values"""
        runner = NodeRunner()

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
            name="custom_node", logger=mock_logger, task_id="custom_123"
        )

        assert runner.name == "custom_node"
        assert runner.task_id == "custom_123"
        assert runner.logger == mock_logger

    def test_debug_logging(self, node_runner, mock_logger):
        """Test debug logging method"""
        node_runner.debug("test debug message")

        mock_logger.debug.assert_called_once_with(
            "Task test_node: test debug message for task_id: test_123"
        )

    def test_info_logging(self, node_runner, mock_logger):
        """Test info logging method"""
        node_runner.info("test info message")

        # Should be called twice - once in __init__ and once in our test
        assert mock_logger.info.call_count == 2
        mock_logger.info.assert_called_with(
            "Task test_node: test info message task_id: test_123"
        )

    def test_warning_logging(self, node_runner, mock_logger):
        """Test warning logging method"""
        node_runner.warning("test warning message")

        mock_logger.warning.assert_called_once_with(
            "Task test_node: test warning message task_id: test_123"
        )

    def test_error_logging(self, node_runner, mock_logger):
        """Test error logging method"""
        node_runner.error("test error message")

        mock_logger.error.assert_called_once_with(
            "Task test_node: test error message task_id: test_123"
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
        mock_subprocess.assert_called_once_with(
            "echo 'hello'", shell=True, capture_output=True, text=True, cwd=None
        )

        # Check that log file was created and FileStack was added
        mock_file.assert_called_with("test_command.log", "w")
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

        mock_file.assert_called_with("process.log", "w")
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
        mock_logger.exception.assert_called_with(
            "Task test_node: Something went wrong task_id: test_123"
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
            call("Task test_node: started task_id: test_123"),
            # Second call from succeed
            call(
                "Task test_node: succeeded Task completed successfully task_id: test_123"
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
        """Test that task_id is properly extracted from kwargs"""
        runner = NodeRunner(
            name="test", logger=mock_logger, task_id="specific_id", other_param="value"
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
                name="integration_test", logger=mock_logger, task_id="int_123"
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
                name="integration_fail", logger=mock_logger, task_id="fail_123"
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
