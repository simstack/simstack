import argparse
import asyncio
import importlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional, Type
import fnmatch  # <-- added

from simstack.core.context import context
from simstack.core.find_simstack_modules import find_simstack_modules
from simstack.util.db import Database
from simstack.util.import_module import import_module_from_file
from simstack.util.path_manager import path_manager



class TableBuilderBase(ABC):
    """
    Shared pipeline for building "tables" by scanning:
      1) installed simstack modules (packages)
      2) configured project paths (python files)

    Subclasses only implement `_process_module(module, drops)`.
    """

    def __init__(self, db: Database, write_schema: bool = False, project_root: Optional[Path] = None):
        self.db = db
        self.write_schema = write_schema
        self._project_root = project_root

    @property
    def project_root(self) -> Path:
        if self._project_root:
            return self._project_root
        if context.config:
            return context.config.project_root
        from simstack.util.project_root_finder import find_project_root
        return find_project_root()

    @property
    @abstractmethod
    def logger(self) -> logging.Logger:
        raise NotImplementedError

    async def build(
        self,
        *,
        dirs: Optional[list[Path]] = None,
        drops: str = "",
        exclude: Optional[list[str]] = None,
        clear: bool = False,
    ) -> None:
        """
        Build the table.

        - If `dirs` is None: use configured `path_manager` paths (existing behavior).
        - If `dirs` is a list: scan those directories for Python files and process them.

        `exclude` entries can match:
          - path parts (e.g. ".venv", "__pycache__")
          - glob patterns (e.g. "*.generated.py")
          - nested relative paths (e.g. "src/simstack/models")
        """
        await self._ensure_context_initialized()
        if clear:
            await self.clear_table()

        await self._process_simstack_modules(drops=drops)

        if dirs is None:
            raise ValueError("dirs must be specified")
        else:
            await self._process_dirs(dirs, drops=drops, exclude=exclude or [])

    async def _ensure_context_initialized(self) -> None:
        if not context.initialized:
            await context.initialize()

    async def _process_simstack_modules(self, drops: str) -> None:
        all_modules = set(find_simstack_modules())
        for module_name in all_modules:
            self.logger.debug("Processing module: %s", module_name)
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

    async def _process_dirs(self, dirs: list[Path], *, drops: str, exclude: list[str]) -> None:
        for base_dir in dirs:
            self.logger.info("Processing CLI dir: %s", base_dir)

            base_dir = Path(base_dir)

            # Accept either absolute paths or paths relative to project root.
            base_dir_path = base_dir if base_dir.is_absolute() else (self.project_root / base_dir)

            for py_file in self._iter_python_files_under_dir(base_dir_path, exclude=exclude):
                await self._process_file(py_file, drops)

    def _iter_python_files_under_dir(self, base_dir: Path, *, exclude: list[str]) -> Iterable[Path]:
        default_exclude_parts = {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "__init__.py"
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ipynb_checkpoints"
        }

        def _should_exclude(p: Path) -> bool:
            # Fast path: ignore common tooling/cache dirs anywhere in the path.
            if any(part in default_exclude_parts for part in p.parts):
                return True

            # If the caller didn't pass any excludes, we're done.
            if not exclude:
                return False

            # Match excludes against path parts and relative path (for nested patterns).
            try:
                rel = p.relative_to(base_dir)
            except ValueError:
                rel = p

            rel_posix = rel.as_posix()

            for ex in exclude:
                ex = (ex or "").strip()
                if not ex:
                    continue

                # 1) Exact match against any path segment (works for "build", ".tox", etc.)
                if ex in p.parts or ex in rel.parts:
                    return True

                # 2) Glob match against the relative posix path (supports nested paths + file globs)
                #    Examples:
                #      --exclude "src/simstack/models"
                #      --exclude "**/generated/**"
                #      --exclude "*.generated.py"
                if fnmatch.fnmatch(rel_posix, ex) or fnmatch.fnmatch(rel_posix, ex.rstrip("/") + "/*"):
                    return True

                # 3) Also allow Windows-ish inputs like "a\\b\\c" by normalizing to posix.
                ex_posix = ex.replace("\\", "/")
                if ex_posix != ex and (
                    fnmatch.fnmatch(rel_posix, ex_posix) or fnmatch.fnmatch(rel_posix, ex_posix.rstrip("/") + "/*")
                ):
                    return True

            return False

        if base_dir.is_file() and base_dir.suffix == ".py":
            if not _should_exclude(base_dir):
                yield base_dir
            return

        if not base_dir.exists():
            self.logger.warning("Skipping non-existent path: %s", base_dir)
            return

        if not base_dir.is_dir():
            self.logger.warning("Skipping non-directory path: %s", base_dir)
            return

        for p in base_dir.rglob("*.py"):
            if _should_exclude(p):
                continue
            if p.is_file():
                yield p

    async def _process_file(self, file_path: Path, drops: str) -> None:
        self.logger.debug("Processing file: %s", file_path)
        module = import_module_from_file(file_path, self.project_root)
        if not module:
            self.logger.debug("Skipping %s because module import returned None", file_path)
            return
        await self._process_module(module, drops)

    @abstractmethod
    async def _process_module(self, module, drops: str) -> None:
        """Subclass hook: scan/register whatever you need from `module`."""
        raise NotImplementedError

    async def second_stage(self, drops: str) -> None:
        """ an optional hook to run after all modules have been processed"""
        pass

    async def clear_table(self) -> None:
        """ an optional hook to clear the table before building """
        pass

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
            help="Path to scan (repeatable). If omitted, CWD is used.",
        )
        parser.add_argument(
            "--exclude",
            dest="exclude",
            action="append",
            default=[],
            help=(
                "Exclude a directory/name/glob from scanning (repeatable). "
          
                "Examples: --exclude .venv --exclude __pycache__ --exclude src/simstack/models --exclude '*.generated.py'"
            ),
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
        parser.add_argument(
            "--clear",
            dest="clear",
            action="store_true",
            help="Clear the table before building.",
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
            builder = builder_cls(context.db, write_schema=args.write_schema)
            await builder.build(dirs=dirs, drops=args.drops, exclude=args.exclude, clear=args.clear)
            await builder.second_stage(args.drops)
        loop.run_until_complete(_run())
        loop.close()
