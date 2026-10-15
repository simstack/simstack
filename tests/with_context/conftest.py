from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from simstack.core.context import context
from simstack.core.resources import allowed_resources
from simstack.models import ModelMapping
from simstack.models.files import FileStack
from simstack.models.resource_definition import ResourceDefinition
from simstack.persistence import eq
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table
from simstack.util.config_reader import ConfigReader
from tests.support.in_memory_repository import InMemoryRepository


def pytest_report_header(config):
    return "SIMSTACK: explicit test-only InMemoryRepository"


@pytest_asyncio.fixture(autouse=True, scope="session", loop_scope="session")
async def initialized_context(tmp_path_factory):
    """Initialize domain tests through the repository port only.

    Mongo integration is covered by simstack-server.  The shared-package suite
    never constructs a DB client or patches an ODM/engine.
    """

    working_dir = tmp_path_factory.mktemp("simstack_test")
    home_dir = working_dir / "home"
    (home_dir / "simstack").mkdir(parents=True)
    ssh_dir = home_dir / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").touch()

    project_root = Path.cwd()
    test_workdir = tmp_path_factory.mktemp("test_workdir")
    repository = InMemoryRepository()

    context._runner_boundary = False
    context._initialized = False
    await context.initialize(
        repository=repository,
        skip_config=True,
        is_test=True,
        resource="self",
        workdir=working_dir,
        project_root=project_root,
        log_level="ERROR",
    )

    test_resource = ResourceDefinition(
        resource_str="test",
        workdir=str(test_workdir),
        hostname="localhost",
        is_default=False,
        git_branch="main",
    )
    await repository.save(test_resource)
    await context.initialize_configs(
        repository,
        None,
        resource="test",
        workdir=test_workdir,
        project_root=project_root,
        python_paths=[project_root / "src"],
        environment_start="",
        is_test=True,
    )

    dirs = ["tests", "src/simstack/models", "src/simstack/methods"]
    await make_model_table(
        repository,
        dirs=dirs,
        drops="src",
        clear=True,
        project_root=project_root,
        ignore_entrypoints=True,
    )
    await make_node_table(
        repository,
        dirs=dirs,
        drops="src",
        clear=True,
        project_root=project_root,
        ignore_entrypoints=True,
    )
    await context.refresh_mappings()

    assert await repository.find_one(ModelMapping, eq("name", "FloatData"))

    allowed_resources.clear_resources()
    context.config = await ConfigReader.create(
        "test",
        repository,
        None,
        project_root=project_root,
        workdir=working_dir,
        python_paths=[project_root / "src"],
        environment_start="",
    )

    yield context

    allowed_resources.clear_resources()
    await repository.close()
    context._repository = None
    context._initialized = False
    context._model_mappings = None
    context._node_mappings = None
    context._resource_config = None
    context._config = None


@pytest.fixture(scope="session")
def repository(initialized_context):
    return initialized_context.repository


@pytest.fixture
def test_file_stack():
    test_content = "test content"
    temp_file = context.config.workdir / "test_file.txt"
    temp_file.write_text(test_content)
    file_stack = FileStack.from_local_file(
        temp_file,
        in_memory=True,
        is_hashable=True,
    )
    yield file_stack
    temp_file.unlink(missing_ok=True)
