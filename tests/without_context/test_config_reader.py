from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from simstack.core.resources import allowed_resources
from simstack.models.parameters import Resource
from simstack.models.resource_definition import ResourceDefinition
from simstack.util.config_reader import ConfigReader
from tests.support.in_memory_repository import InMemoryRepository


@pytest.fixture(autouse=True)
def reset_resources():
    allowed_resources.clear_resources()
    yield
    allowed_resources.clear_resources()


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> InMemoryRepository:
    value = InMemoryRepository()
    for resource_name in ("local", "uploads"):
        await value.save(
            ResourceDefinition(
                resource_str=resource_name,
                workdir=str(tmp_path / resource_name),
                python_paths=[],
                hostname="localhost",
                environment_start="",
                routes=[],
            )
        )
    return value


@pytest.mark.asyncio
async def test_create_uses_repository_resource_and_explicit_overrides(
    repository: InMemoryRepository,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "override-workdir"
    workdir.mkdir()
    python_path = tmp_path / "python"
    python_path.mkdir()

    reader = await ConfigReader.create(
        "local",
        repository,
        None,
        tmp_path,
        workdir=workdir,
        python_paths=[python_path],
        environment_start="source env.sh",
        server_url="https://server.example",
        server_token="token",
    )

    assert reader.resource == Resource(value="local")
    assert reader.workdir == workdir
    assert reader.python_paths == [python_path]
    assert reader.environment_start == "source env.sh"
    assert reader.server_url == "https://server.example"
    assert reader.server_token == "token"
    assert set(allowed_resources.get_resources()) == {"local", "uploads", "self"}


@pytest.mark.asyncio
async def test_self_resource_is_derived_without_persistence_fallback(
    repository: InMemoryRepository,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "self-workdir"
    workdir.mkdir()
    reader = await ConfigReader.create(
        "self",
        repository,
        None,
        tmp_path,
        workdir=workdir,
        python_paths=[],
        environment_start="",
    )

    assert reader.resource == Resource(value="self")
    assert reader.hostname == "localhost"
    assert reader.workdir == workdir


@pytest.mark.asyncio
async def test_create_rejects_unknown_resource(
    repository: InMemoryRepository,
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="Resource missing not found in the list of allowed resources",
    ):
        await ConfigReader.create(
            "missing",
            repository,
            None,
            tmp_path,
            workdir=tmp_path,
            python_paths=[],
            environment_start="",
        )


@pytest.mark.asyncio
async def test_resource_property_is_read_only(
    repository: InMemoryRepository,
    tmp_path: Path,
) -> None:
    reader = await ConfigReader.create(
        "local",
        repository,
        None,
        tmp_path,
        workdir=tmp_path,
        python_paths=[],
        environment_start="",
    )

    with pytest.raises(ValueError, match="cannot be set directly"):
        reader.resource = "uploads"
