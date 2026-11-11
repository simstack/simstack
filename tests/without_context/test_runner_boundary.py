import ast
from pathlib import Path

import pytest
import simstack
from simstack.core.context import GlobalState


def test_runner_distribution_has_no_storage_backend_or_facade():
    package = Path(simstack.__file__).parent
    removed = ["util/db.py", "util/database_information.py", "runner_api/repository.py",
               "persistence/criteria.py", "persistence/memory.py"]
    assert not any((package / name).exists() for name in removed)
    forbidden = ("simstack.util.db", "simstack.persistence", "simstack.runner_api.repository", "motor", "pymongo", "odmantic")
    for path in package.rglob("*.py"):
        if "static" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden), path
            if isinstance(node, ast.Import):
                assert not any(item.name.startswith(forbidden) for item in node.names), path
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "context":
                assert node.attr not in {"db", "_db", "store", "repository", "engine", "core_engine"}, path


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["db_name", "db_type", "connection_string", "database"])
async def test_database_configuration_is_rejected(key):
    with pytest.raises(ValueError, match="database configuration"):
        await GlobalState().initialize(**{key: "unsupported"})
