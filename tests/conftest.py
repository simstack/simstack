import sys
from os import mkdir
from pathlib import Path

import pytest
import pytest_asyncio
import asyncio
from simstack.core.context import context
from simstack.core.definitions import DBType
from simstack.core.model_table import make_model_table
from simstack.core.node_table import make_node_table
from simstack.models.files import FileStack
from simstack.util.project_root_finder import find_project_root

import threading
import queue


@pytest.fixture(scope="session")
def event_loop():
    # This event_loop is required because the default pytest-asyncio event loop is function scoped
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    # loop.close()

