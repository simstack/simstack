"""Install only explicitly configured workflow import roots in every process."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .config import RunnerConfig


def configure_runner_import_paths(config: RunnerConfig) -> None:
    """Make declared workflow locations independent of the current task cwd.

    No source-directory guessing or inherited PYTHONPATH is used. Bootstrap
    supplies the resource paths and the supervisor supplies the project root.
    """
    paths = list(dict.fromkeys(
        str(Path(path).resolve())
        for path in (config.project_root, *config.python_paths)
    ))
    sys.path[:] = paths + [path for path in sys.path if path not in paths]
    importlib.invalidate_caches()
