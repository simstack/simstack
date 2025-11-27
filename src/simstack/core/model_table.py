import argparse
import asyncio
import importlib
import inspect
import json
import logging
import os
from pathlib import Path

from simstack.core.find_simstack_modules import find_simstack_modules
from simstack.models.models import ModelMapping
from simstack.models.simstack_model import is_simstack_model
from simstack.util.import_module import import_module_from_file
from simstack.util.project_root_finder import find_project_root

logger = logging.getLogger("ModelTable")


class CreateModelTable:
    """
    Helper class to build the model table without passing around many parameters.

    Usage:
        creator = CreateModelTable(engine)
        await creator.make_model_table()
    """

    def __init__(self, engine):
        from simstack.core.context import context

        # Ensure context is initialized and store frequently used objects
        if not context.initialized:
            context.initialize()

        self.context = context
        self.engine = engine
        self.path_manager = context.path_manager

    async def make_model_table(self):
        """Entry point to (re)build the model table."""
        # First, process all simstack modules
        all_modules = find_simstack_modules()
        for module_name in all_modules:
            logger.info(f"Processing module: {module_name}")
            module = importlib.import_module(module_name)
            await self._create_models_from_module(module, drops="")

        # Then process all configured paths
        for path_name in self.path_manager.paths.keys():
            await self._make_models_for_path(path_name)

    async def _create_model_models_from_file(self, file_path: str, drops: str):
        """Create ModelMapping entries for classes in the specified Python file."""
        logger.debug(f"Processing models from: {file_path}")
        module = import_module_from_file(Path(file_path))
        if not module:
            return

        await self._create_models_from_module(module, drops)

    async def _create_models_from_module(self, module, drops: str):
        """Create ModelMapping entries for all relevant classes in a module."""
        classes = inspect.getmembers(module, inspect.isclass)

        for class_name, new_class in classes:
            # this is required because of the Odmantic Metaclass Model
            # subclass does not work even if applied to the imported classes
            # this is a bug in importlib
            bases = [base.__name__ for base in new_class.__bases__]
            is_ui_model = any("UIModel" in s for s in bases) or is_simstack_model(
                new_class
            )
            is_model = any(s == "Model" for s in bases)

            is_embedded_model = any(s == "EmbeddedModel" for s in bases)

            if not (is_model or is_ui_model):
                continue
            if class_name == "Model":
                continue
            if new_class.__module__ != module.__name__:
                continue

            new_modules = new_class.__module__.split(".")
            # Use drops from the path_info dictionary
            if drops != "":
                drop_modules = drops.split(".")
                while (
                    new_modules and drop_modules and new_modules[0] == drop_modules[0]
                ):
                    new_modules.pop(0)
                    drop_modules.pop(0)
                if len(drop_modules) > 0:
                    raise ValueError(
                        "drop modules not empty: ", drop_modules, new_class.__module__
                    )
            full_mapping = ".".join(new_modules) + "." + class_name
            logger.debug(f"    Class: {class_name} Model Mapping: {full_mapping}")

            # Remove any existing ModelMapping entry for this class
            existing_entry = await self.engine.find_one(
                ModelMapping, ModelMapping.name == class_name
            )
            if existing_entry is not None:
                await self.engine.delete(existing_entry)
                logger.debug(f"Deleted ModelMapping entry for {class_name}")

            # EmbeddedModels have no collection by may be simstack_models. They are never saved/retrieved
            collection_name = getattr(new_class, "__collection__", None)
            if collection_name is None:
                if is_embedded_model:
                    collection_name = f"EmbeddedModel"
                else:
                    logger.error(f"No collection specified for {class_name}")

            # Create the new ModelMapping entry (pickle functionality removed)
            if is_ui_model:
                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                    collection_name=collection_name,
                    json_schema=json.dumps(new_class.json_schema()),
                    ui_schema=json.dumps(new_class.ui_schema()),
                    route="",
                )
                logger.debug(f"SimStack Model: {class_name} Mapping: {full_mapping} Collection: {collection_name}")
                # open a file in a subdirectory of the current file schema/model.json
                project_root = find_project_root()
                json_file_dir = os.path.join(project_root, "schema")
                os.makedirs(json_file_dir, exist_ok=True)
                combined_schema = {
                    "json_schema": new_class.json_schema(),
                    "ui_schema": new_class.ui_schema(),
                }
                with open(os.path.join(json_file_dir, class_name + ".json"), "w") as f:
                    f.write(json.dumps(combined_schema, indent=4))
            else:
                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                    collection_name=collection_name,
                )
                logger.debug(f"Model: {class_name} Mapping: {full_mapping} Collection: {collection_name}")

            await self.engine.save(model_entry)

    async def _make_models_for_path(self, path_name: str):
        """Build model mappings for all Python files under a configured path."""
        path_info = self.path_manager.get_path(path_name)
        path = path_info["path"]
        drops = path_info["drops"]
        logger.info(f"Making model_table entries for files in {path}")

        # Process each file in this path
        for file_path in self.path_manager.find_python_files(path_name):
            await self._create_model_models_from_file(file_path, drops)


# Public API preserved for existing callers (e.g. tests)
async def make_model_table(engine):
    """
    Rebuild the model table using the given engine.

    This is a thin wrapper around CreateModelTable for backward compatibility.
    """
    creator = CreateModelTable(engine)
    await creator.make_model_table()


def create_model_table_main():
    # Don't create a new loop with asyncio.run, use an existing one
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Initialize context with this loop
    from simstack.core.context import context

    context.initialize(log_level=level)

    # Set pymongo logger level to INFO
    logging.getLogger("pymongo").setLevel(logging.INFO)

    # Run in the same loop
    loop.run_until_complete(make_model_table(context.db.engine))
    loop.close()


if __name__ == "__main__":
    create_model_table_main()
