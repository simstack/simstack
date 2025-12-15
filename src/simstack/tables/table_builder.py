import argparse
import asyncio
import importlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional, Type

from simstack.core.context import context
from simstack.core.engine import AIOEngineProxy
from simstack.core.find_simstack_modules import find_simstack_modules
from simstack.util.import_module import import_module_from_file
from simstack.util.path_manager import path_manager



class TableBuilderBase(ABC):
    """
    Shared pipeline for building "tables" by scanning:
      1) installed simstack modules (packages)
      2) configured project paths (python files)

    Subclasses only implement `_process_module(module, drops)`.
    """

    def __init__(self, engine: AIOEngineProxy, write_schema: bool = False):
        self.engine = engine
        self.write_schema = write_schema

    @property
    @abstractmethod
    def logger(self) -> logging.Logger:
        raise NotImplementedError

    async def build(
        self,
        *,
        dirs: Optional[list[Path]] = None,
        drops: str = "",
    ) -> None:
        """
        Build the table.

        - If `dirs` is None: use configured `path_manager` paths (existing behavior).
        - If `dirs` is a list: scan those directories for Python files and process them.
        """
        await self._ensure_context_initialized()
        await self._process_simstack_modules(drops=drops)

        if dirs is None:
            raise ValueError("dirs must be specified")
        else:
            await self._process_dirs(dirs, drops=drops)

    async def _ensure_context_initialized(self) -> None:
        if not context.initialized:
            await context.initialize()

    async def _process_simstack_modules(self, drops: str) -> None:
        for module_name in find_simstack_modules():
            self.logger.info("Processing module: %s", module_name)
            module = self._import_package_module(module_name)
            if module is None:
                continue
            await self._process_module(module, drops=drops)

    def _import_package_module(self, module_name: str):
        try:
            return importlib.import_module(module_name)
        except Exception as exc:
            self.logger.warning("Failed to import module %s: %s", module_name, exc)
            return None

    async def _process_configured_paths(self) -> None:
        for path_name in path_manager.paths.keys():
            await self._process_path(path_name)

    async def _process_path(self, path_name: str) -> None:
        path_info = path_manager.get_path(path_name)
        base_path = path_info["path"]
        drops = path_info.get("drops", "")

        self.logger.info("Processing configured path %s: %s", path_name, base_path)

        for file_path in path_manager.find_python_files(path_name):
            await self._process_file(file_path, drops)

    async def _process_dirs(self, dirs: list[Path], *, drops: str) -> None:
        """
        Process CLI-provided paths.

        Includes subdirectories by recursively scanning for *.py under each directory.
        Accepts both directories and single .py files.
        """
        for base_dir in dirs:
            base_dir_path = Path(base_dir)

            # If relative, treat it as relative to project_root (consistent with how imports are computed)
            if not base_dir_path.is_absolute():
                base_dir_path = context.config.project_root / base_dir_path

            self.logger.info("Processing CLI path: %s", base_dir_path)

            for py_file in self._iter_python_files_under_dir(base_dir_path):
                await self._process_file(py_file, drops)

    def _iter_python_files_under_dir(self, base_dir: Path) -> Iterable[Path]:
        exclude_parts = {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
        }

        if base_dir.is_file() and base_dir.suffix == ".py":
            yield base_dir
            return

        if not base_dir.exists():
            self.logger.warning("Skipping non-existent path: %s", base_dir)
            return

        if not base_dir.is_dir():
            self.logger.warning("Skipping non-directory path: %s", base_dir)
            return

        # Recursively include subdirectories
        for p in base_dir.rglob("*.py"):
            if any(part in exclude_parts for part in p.parts):
                continue
            if p.is_file():
                yield p

    async def _process_file(self, file_path: Path, drops: str) -> None:
        self.logger.debug("Processing file: %s", file_path)
        module = import_module_from_file(file_path, context.config.project_root)
        if not module:
            self.logger.debug("Skipping %s because module import returned None", file_path)
            return
        await self._process_module(module, drops)

    @abstractmethod
    async def _process_module(self, module, drops: str) -> None:
        """Subclass hook: scan/register whatever you need from `module`."""
        raise NotImplementedError

    @classmethod
    def cli_main(cls, builder_cls: Type["TableBuilderBase"], write_schema: bool = False) -> None:
        """
        Reusable CLI entry point.

        Options:
          --dir RELPATH   (repeatable) Directories (relative to CWD) to scan for *.py files.
                         If omitted, defaults to scanning the current working directory.
          --drops STRING  Drops prefix (string) applied while processing these CLI dirs.
          -v/--verbose    Increase logging verbosity.
        """
        parser = argparse.ArgumentParser()
        parser.add_argument("-v", "--verbose", action="count", default=0)
        parser.add_argument(
            "--dir",
            dest="dirs",
            action="append",
            default=[],
            help="Relative path to CWD to scan (repeatable). If omitted, CWD is used.",
        )
        parser.add_argument(
            "--drops",
            dest="drops",
            default="",
            help="Drops prefix used when processing CLI dirs (string).",
        )
        parser.add_argument(
            "--write-schema",
            dest="write_schema",
            action="store_true",
            help="Enable schema writing.",
        )

        args = parser.parse_args()

        level = logging.WARNING
        if args.verbose == 1:
            level = logging.INFO
        elif args.verbose >= 2:
            level = logging.DEBUG

        # Resolve dirs relative to the current working directory
        if args.dirs:
            dirs = [Path.cwd() / d for d in args.dirs]
        else:
            dirs = [Path.cwd()]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        logging.getLogger("pymongo").setLevel(logging.INFO)

        async def _run() -> None:
            await context.initialize(log_level=level, resource="self")
            builder = builder_cls(context.db.engine, write_schema=args.write_schema)
            await builder.build(dirs=dirs, drops=args.drops)

        loop.run_until_complete(_run())
        loop.close()
