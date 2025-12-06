import pytest

from simstack.core.node import node
from simstack.core.node_runner import NodeRunner
from simstack.models.files import FileStack

import logging

from simstack.models import StringData

logger = logging.getLogger("FileArgumentNode")


@node
def run_file_argument(file_stack: FileStack, **kwargs):
    """
    A node that runs a file argument.
    :param file_stack: FileStack object containing the file to be processed.
    :return: A success message with the file stack ID.
    """
    task_id = kwargs.get("task_id", None)

    node_runner = NodeRunner(name="run_file_argument", **kwargs)

    node_runner.info(f"Processing file stack: {file_stack.id} with task_id: {task_id}")
    node_runner.value = StringData(value="str(self.id")

    return node_runner.succeed(f"File {file_stack.id} processed successfully.")


@pytest.mark.asyncio
async def test_file_rerun(caplog, test_file_stack):
    with caplog.at_level(logging.INFO):
        result1 = run_file_argument(test_file_stack)
        assert "Processing file stack" in caplog.text
        assert isinstance(result1, StringData)
        run_id = result1.value

        caplog.clear()
        result2 = run_file_argument(test_file_stack)
        assert "Processing file stack" not in caplog.text
        assert isinstance(result2, StringData)
        assert result2.value == run_id
