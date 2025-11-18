import inspect
import json
import os
import asyncio
import pkgutil
import importlib
from pathlib import Path
from simstack.models.models import ModelMapping
from simstack.models.pickle_models import ClassPickle
from simstack.models.simstack_model import is_simstack_model
from simstack.core.node_table import import_module_from_file
from simstack.core.find_simstack_modules import find_simstack_modules
import logging

logger = logging.getLogger("ModelTable")

def find_tuple_by_first_element(tuples_list, target_string):
    for tup in tuples_list:
        if tup[0] == target_string:
            return tup[1]
    return None  # Return None if no match is found

async def make_model_table(engine):
    from simstack.core.context import context
    if not context.initialized:
        context.initialize()

    all_modules = find_simstack_modules()
    for module in all_modules:
        logger.info(f"Processing module: {module}")
        module = importlib.import_module(module)
        await create_models_from_module(module, engine, '')

    # Iterate over all paths in the PathManager
    for path_name in context.path_manager.paths.keys():
        await make_models_for_path(path_name, context.path_manager, engine)


async def create_model_models_from_file(file_path: str, engine, drops: str, use_pickle: bool = False):
    """Create ModelMapping entries for classes in the specified Python file."""
    logger.debug(f"Processing models from: {file_path}")
    module = import_module_from_file(Path(file_path))
    if not module:
        return

    await create_models_from_module(module, engine, drops, use_pickle)

async def create_models_from_module(module, engine, drops: str, use_pickle: bool = False):
    classes = inspect.getmembers(module, inspect.isclass)

    for class_name, new_class in classes:
        # this is required because of the Odmantic Metaclass Model
        # subclass does not work even if applied to the imported classes
        # this is a bug in importlib
        bases = [base.__name__ for base in new_class.__bases__]
        is_ui_model = any("UIModel" in s for s in bases) or is_simstack_model(new_class)
        is_model = any(s == "Model" for s in bases)

        if not (is_model or is_ui_model):
            continue
        if class_name == "Model":
            continue
        if new_class.__module__ != module.__name__:
            continue

        new_modules = new_class.__module__.split(".")
        # Use drops from the path_info dictionary
        if drops != '':
            drop_modules = drops.split(".")
            while new_modules and drop_modules and new_modules[0] == drop_modules[0]:
                new_modules.pop(0)
                drop_modules.pop(0)
            if len(drop_modules) > 0:
                raise ValueError("drop modules not empty: ", drop_modules, new_class.__module__)
        full_mapping = ".".join(new_modules) + "." + class_name
        logger.debug(f"    Class: {class_name} Model Mapping: {full_mapping}")
        # Find existing ModelMapping entry
        existing_entry = await engine.find_one(ModelMapping, ModelMapping.name == class_name)
        if existing_entry is not None:
            # If it has a pickle_class, delete the corresponding ClassPickle
            if existing_entry.pickle_class:
                try:
                    # Delete the ClassPickle directly using the reference
                    await engine.delete(existing_entry.pickle_class)
                    logger.debug(f"Deleted ClassPickle for {class_name}")
                except Exception as e:
                    logger.error(f"Error deleting ClassPickle for {class_name}: {e}")

            # Delete the ModelMapping entry
            await engine.delete(existing_entry)
            logger.debug(f"Deleted ModelMapping entry for {class_name}")
        # Create a ClassPickle instance only if use_pickle is true for this path
        class_pickle = None
        if use_pickle and class_name != "ClassPickle":  # Don't pickle the ClassPickle class itself
            try:
                # Create a ClassPickle instance
                class_pickle = ClassPickle(
                    name=class_name,
                    module_path=new_class.__module__
                )

                # Store the class
                class_pickle.store_class(new_class)

                # Save the ClassPickle instance
                class_pickle = await engine.save(class_pickle)

                logger.debug(f"Created ClassPickle for {class_name}")
            except Exception as e:
                logger.error(f"Error creating ClassPickle for {class_name}: {e}")
                class_pickle = None

        if is_ui_model:
            model_entry = ModelMapping(
                name=class_name,
                mapping=full_mapping,
                collection_name=getattr(new_class, "__collection__", None),
                json_schema=json.dumps(new_class.json_schema()),
                ui_schema=json.dumps(new_class.ui_schema()),
                route="",
                pickle_class=class_pickle
            )
            logger.debug("SimStack Model: ", class_name, "Mapping: ", full_mapping)
            # open a file in a subdirectory of the current file schema/model.json
            json_file_dir = os.path.join(os.path.dirname(__file__), "schema")
            os.makedirs(json_file_dir, exist_ok=True)
            combined_schema = {
                "json_schema": new_class.json_schema(),
                "ui_schema": new_class.ui_schema()
            }
            with open(os.path.join(json_file_dir, class_name + ".json"), "w") as f:
                f.write(json.dumps(combined_schema, indent=4))
        else:
            model_entry = ModelMapping(
                name=class_name,
                mapping=full_mapping,
                collection_name=getattr(new_class, "__collection__", None),
                pickle_class=class_pickle
            )
            logger.debug("Model: ", class_name, "Mapping: ", full_mapping)
        new_entry = await engine.save(model_entry)


async def make_models_for_path(path_name, path_manager, engine):
    path_info = path_manager.get_path(path_name)
    path = path_info["path"]
    drops = path_info["drops"]
    use_pickle = path_info.get("use_pickle", False)
    logger.info(f"Making model_table entries for files in {path}")
    # Process each file in this path
    for path in path_manager.find_python_files(path_name):
        await create_model_models_from_file(path, engine, drops, use_pickle)


def create_model_table_main():
    # Don't create a new loop with asyncio.run, use an existing one
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Initialize context with this loop
    from simstack.core.context import context
    context.initialize()

    # Run in the same loop
    loop.run_until_complete(make_model_table(context.db.engine))
    loop.close()

if __name__ == "__main__":
    create_model_table_main()
