import os

import pytest
from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node
from simstack.core.node_runner import NodeRunner
from simstack.models import NodeRegistry, IntData
from simstack.models.files import FileStack


@node
def failing_node(arg: IntData, **kwargs) -> IntData:
    task_id = kwargs.get("task_id", None)
    raise RuntimeError(f"Task task_id: {task_id} This is a test exception")


@pytest.fixture()
def info_file():
    info_file_path = context.config.workdir / "test_info.json"
    with open(info_file_path, "w") as f:
        f.write("{}")
    info_file_stack = FileStack.from_local_file(info_file_path, in_memory=True)
    yield info_file_stack
    os.unlink(info_file_path)


@pytest.fixture()
def hello_world_file():
    hello_world_file_path = context.config.workdir / "hello_world.txt"
    with open(hello_world_file_path, "w") as f:
        f.write("Hello World!")
    hello_world_file_stack = FileStack.from_local_file(
        hello_world_file_path, in_memory=True
    )
    yield hello_world_file_stack
    os.unlink(hello_world_file_path)


@node
def failing_node_with_runner(
    info_file: FileStack, hello_world_file: FileStack, **kwargs
) -> IntData:
    node_runner: NodeRunner | None = kwargs.get("node_runner", None)
    if node_runner is None:
        raise RuntimeError("No node runner found")
    node_runner.info_files.append(info_file)
    node_runner.files.append(hello_world_file)
    raise RuntimeError(f"Task task_id: {node_runner.task_id} This is a test exception")


@node
def calling_failing_node(arg: IntData, **kwargs) -> IntData:
    failing_node(arg, **kwargs)
    return IntData(value=arg.value + 1)


def node_returns_nothing(arg: IntData, **kwargs):
    pass


@node
def node_returns_bool(arg: IntData, **kwargs) -> bool:
    return arg.value == 1


def test_failing_node():
    with pytest.raises(
        RuntimeError, match=r"Task task_id: .* This is a test exception"
    ):
        failing_node(IntData(value=1))


@pytest.mark.skip(reason="works locally but not in gitlab ci/cd")
def test_calling_failing_node():
    with pytest.raises(
        RuntimeError, match=r"Task task_id: .* This is a test exception"
    ):
        calling_failing_node(IntData(value=1))


def test_node_returns_bool():
    result = node_returns_bool(IntData(value=1))
    assert result is True


def test_node_returns_bool_false():
    result = node_returns_bool(IntData(value=2))
    assert result is None


def test_node_returns_nothing():
    result = node_returns_nothing(IntData(value=1))
    assert result is None


@pytest.mark.asyncio
async def test_failing_node_with_runner(info_file, hello_world_file):
    with pytest.raises(RuntimeError) as exc_info:
        failing_node_with_runner(info_file, hello_world_file)

        # Extract task_id from the error message
    error_message = str(exc_info.value)
    # Parse the task_id from the message (assuming format: "Task task_id: <id> This is a test exception")
    import re

    match = re.search(r"Task task_id: (\S+) This is a test exception", error_message)
    assert match
    task_id = match.group(1)

    node_registry = await context.db.find_one(
        NodeRegistry, NodeRegistry.id == ObjectId(task_id)
    )
    assert len(node_registry.info_files) == 1
    assert len(node_registry.result_names) == 1
    assert node_registry.result_names[0] == "files"
    assert node_registry.status == TaskStatus.FAILED
