import pytest
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from simstack.core.context import context
from simstack.core.node_runner import NodeRunner


class TestMakeInfoFiles:
    """Test suite for make_info_files method"""

    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    @pytest.fixture
    def node_runner(self, mock_logger):
        """Create a NodeRunner instance for testing"""
        return NodeRunner(name="test_node", logger=mock_logger, task_id="test_123")

    @pytest.fixture
    def test_dir(self):
        """Create a test directory with test files in workdir"""

        test_path = Path(context.config.workdir) / "test_make_info_files"
        test_path.mkdir(parents=True, exist_ok=True)

        # Create test files
        (test_path / "test1.log").write_text("log content 1")
        (test_path / "test2.log").write_text("log content 2")
        (test_path / "test1.out").write_text("output content 1")
        (test_path / "test2.out").write_text("output content 2")
        (test_path / "error.err").write_text("error content")
        (test_path / "config.txt").write_text("config content")

        yield test_path

        # Cleanup after test
        if test_path.exists():
            shutil.rmtree(test_path)

    @pytest.mark.asyncio
    async def test_make_info_files_with_existing_files(self, node_runner, test_dir):
        """Test adding existing files directly"""
        file1 = test_dir / "test1.log"
        file2 = test_dir / "config.txt"

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stack1 = MagicMock()
            mock_stack2 = MagicMock()
            mock_file_stack.side_effect = [mock_stack1, mock_stack2]

            await node_runner.make_info_files(str(file1), str(file2))

            assert len(node_runner.info_files) == 2
            assert mock_stack1 in node_runner.info_files
            assert mock_stack2 in node_runner.info_files

            # Verify FileStack.from_local_file was called correctly
            assert mock_file_stack.call_count == 2
            mock_file_stack.assert_any_call(
                str(file1), in_memory=True, is_hashable=True, secure_source=True
            )
            mock_file_stack.assert_any_call(
                str(file2), in_memory=True, is_hashable=True, secure_source=True
            )

    @pytest.mark.asyncio
    async def test_make_info_files_with_patterns(self, node_runner, test_dir):
        """Test adding files using glob patterns"""
        pattern1 = str(test_dir / "*.log")
        pattern2 = str(test_dir / "*.out")

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stacks = [MagicMock() for _ in range(4)]  # 2 .log + 2 .out files
            mock_file_stack.side_effect = mock_stacks

            await node_runner.make_info_files(pattern1, pattern2)

            assert len(node_runner.info_files) == 4
            assert mock_file_stack.call_count == 4

            # Verify patterns were added to info_file_patterns
            assert pattern1 in node_runner.info_file_patterns
            assert pattern2 in node_runner.info_file_patterns

    @pytest.mark.asyncio
    async def test_make_info_files_mixed_args(self, node_runner, test_dir):
        """Test mixing direct files and patterns"""
        direct_file = test_dir / "config.txt"
        pattern = str(test_dir / "*.err")

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stacks = [MagicMock(), MagicMock()]
            mock_file_stack.side_effect = mock_stacks

            await node_runner.make_info_files(str(direct_file), pattern)

            assert len(node_runner.info_files) == 2
            assert pattern in node_runner.info_file_patterns

    @pytest.mark.asyncio
    async def test_make_info_files_nonexistent_file(self, node_runner, test_dir):
        """Test with non-existent file"""
        nonexistent_file = test_dir / "nonexistent.txt"
        existing_file = test_dir / "test1.log"

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stack = MagicMock()
            mock_file_stack.return_value = mock_stack

            await node_runner.make_info_files(str(nonexistent_file), str(existing_file))

            # Only existing file should be processed
            assert len(node_runner.info_files) == 1
            assert mock_file_stack.call_count == 1
            mock_file_stack.assert_called_with(
                str(existing_file), in_memory=True, is_hashable=True, secure_source=True
            )

    @pytest.mark.asyncio
    async def test_make_info_files_unreadable_file(self, node_runner, test_dir):
        """Test with unreadable file"""
        unreadable_file = test_dir / "unreadable.txt"
        unreadable_file.write_text("content")

        with patch("os.access", return_value=False):
            with patch(
                "simstack.models.files.FileStack.from_local_file"
            ) as mock_file_stack:
                await node_runner.make_info_files(str(unreadable_file))

                # Should not process unreadable file
                assert len(node_runner.info_files) == 0
                assert mock_file_stack.call_count == 0

    @pytest.mark.asyncio
    async def test_make_info_files_empty_pattern(self, node_runner, test_dir):
        """Test with pattern that matches no files"""
        empty_pattern = str(test_dir / "*.nonexistent")

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            await node_runner.make_info_files(empty_pattern)

            assert len(node_runner.info_files) == 0
            assert empty_pattern in node_runner.info_file_patterns
            assert mock_file_stack.call_count == 0

    @pytest.mark.asyncio
    async def test_make_info_files_duplicate_files(self, node_runner, test_dir):
        """Test that duplicate files are handled correctly (set behavior)"""
        file1 = test_dir / "test1.log"
        # Create a pattern that will match the same file
        pattern = str(file1)  # Direct path, not a glob pattern

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stack = MagicMock()
            mock_file_stack.return_value = mock_stack

            await node_runner.make_info_files(str(file1), pattern)

            # Should only process the file once due to set behavior
            assert len(node_runner.info_files) == 1
            assert mock_file_stack.call_count == 1

    @pytest.mark.asyncio
    async def test_make_info_files_non_string_args(self, node_runner):
        """Test with non-string arguments"""
        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            await node_runner.make_info_files(123, None, True, "valid_string")

            # Should ignore non-string arguments
            assert mock_file_stack.call_count == 0
            assert len(node_runner.info_files) == 0

    @pytest.mark.asyncio
    async def test_make_info_files_exception_handling(self, node_runner, test_dir):
        """Test exception handling in make_info_files"""
        file1 = test_dir / "test1.log"

        with patch(
            "simstack.models.files.FileStack.from_local_file",
            side_effect=Exception("File processing error"),
        ):
            with patch.object(node_runner, "error") as mock_error:
                await node_runner.make_info_files(str(file1))

                # Should handle exception and call error method
                mock_error.assert_called_once()
                assert "Error in finally block" in mock_error.call_args[0][0]

    @pytest.mark.asyncio
    async def test_make_info_files_logging(self, node_runner, test_dir):
        """Test that proper logging occurs"""
        file1 = test_dir / "test1.log"
        pattern = str(test_dir / "*.out")

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stacks = [
                MagicMock() for _ in range(3)
            ]  # 1 direct + 2 pattern matches
            mock_file_stack.side_effect = mock_stacks

            with patch.object(node_runner, "info") as mock_info:
                await node_runner.make_info_files(str(file1), pattern)

                # Should log processing patterns and found files
                assert mock_info.call_count >= 2
                # Check that logging includes pattern info
                pattern_log_found = any(
                    "Processing patterns" in str(call)
                    for call in mock_info.call_args_list
                )
                files_log_found = any(
                    "Found info files" in str(call) for call in mock_info.call_args_list
                )
                assert pattern_log_found
                assert files_log_found

    @pytest.mark.asyncio
    async def test_make_info_files_no_args(self, node_runner):
        """Test with no arguments"""
        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            await node_runner.make_info_files()

            assert len(node_runner.info_files) == 0
            assert mock_file_stack.call_count == 0

    @pytest.mark.asyncio
    async def test_make_info_files_file_becomes_unavailable(
        self, node_runner, test_dir
    ):
        """Test when file exists during initial check but becomes unavailable later"""
        file1 = test_dir / "test1.log"

        # Mock os.path.exists to return True for initial check, False for final check
        exists_calls = [True, False]  # First call returns True, second returns False

        with patch("os.path.exists", side_effect=exists_calls):
            with patch("os.access", return_value=True):
                with patch(
                    "simstack.models.files.FileStack.from_local_file"
                ) as mock_file_stack:
                    await node_runner.make_info_files(str(file1))

                    # Should not process file if it becomes unavailable
                    assert len(node_runner.info_files) == 0
                    assert mock_file_stack.call_count == 0

    @pytest.mark.asyncio
    async def test_make_info_files_workdir_integration(self, node_runner, test_dir):
        """Test that files are correctly found within workdir structure"""
        # Create files in nested structure similar to how nodes work
        nested_dir = test_dir / "nested"
        nested_dir.mkdir()
        (nested_dir / "nested.log").write_text("nested log content")

        pattern = str(nested_dir / "*.log")

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stack = MagicMock()
            mock_file_stack.return_value = mock_stack

            await node_runner.make_info_files(pattern)

            assert len(node_runner.info_files) == 1
            assert mock_file_stack.call_count == 1
            # Verify the correct nested file path was used
            called_path = mock_file_stack.call_args[0][0]
            assert "nested.log" in called_path

    @pytest.mark.asyncio
    async def test_make_info_files_multiple_patterns_same_type(
        self, node_runner, test_dir
    ):
        """Test multiple patterns that might overlap"""
        pattern1 = str(test_dir / "*.log")
        pattern2 = str(test_dir / "test*.log")  # Overlapping pattern

        with patch(
            "simstack.models.files.FileStack.from_local_file"
        ) as mock_file_stack:
            mock_stacks = [
                MagicMock() for _ in range(2)
            ]  # Should only process each file once
            mock_file_stack.side_effect = mock_stacks

            await node_runner.make_info_files(pattern1, pattern2)

            # Set behavior should prevent duplicates
            assert len(node_runner.info_files) == 2  # test1.log and test2.log
            assert mock_file_stack.call_count == 2

            # Both patterns should be stored
            assert pattern1 in node_runner.info_file_patterns
            assert pattern2 in node_runner.info_file_patterns
