import inspect
import json
import logging
from pathlib import Path

from simstack.models.document import Model, EmbeddedModel

from simstack.models.models import ModelMapping
from simstack.models.simstack_model import is_simstack_model
from simstack.tables.catalogue import CatalogueCollector
from simstack.util.path_manager import path_manager
from simstack.tables.table_builder import TableBuilderBase

logger = logging.getLogger("ModelTable")


class CreateModelTable(TableBuilderBase):
    """
    Helper class to build the model table without passing around many parameters.

    Usage:
        creator = CreateModelTable(catalogue)
        await creator.make_model_table()
    """

    @property
    def logger(self) -> logging.Logger:
        return logger

    async def _process_module(self, module, drops: str) -> None:
        await self._create_models_from_module(module, drops)

    async def _create_models_from_module(self, module, drops: str):
        """Create ModelMapping entries for all relevant classes in a module."""
        classes = inspect.getmembers(module, inspect.isclass)

        for class_name, new_class in classes:
            bases = [base.__name__ for base in new_class.__bases__]
            is_ui_model = any("UIModel" in s for s in bases) or is_simstack_model(
                new_class
            )
            is_model = issubclass(new_class, Model)

            is_embedded_model = issubclass(new_class, EmbeddedModel)

            if not (is_model or is_ui_model or is_embedded_model):
                continue
            if new_class in (Model, EmbeddedModel):
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
                # if len(drop_modules) > 0:
                #     raise ValueError(
                #         "drop modules not empty: ", drop_modules, new_class.__module__
                #     )
            full_mapping = ".".join(new_modules) + "." + class_name
            logger.debug(f"    Class: {class_name} Model Mapping: {full_mapping}")

            # Create the new ModelMapping entry (pickle functionality removed)
            if is_ui_model:
                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                    json_schema=json.dumps(new_class.json_schema()),
                    ui_schema=json.dumps(new_class.ui_schema()),
                    route="",
                )
                # open a file in a subdirectory of the current file schema/model.json
                if self.write_schema:
                    project_root = self.project_root
                    json_file_dir = project_root / "schema"
                    json_file_dir.mkdir(parents=True, exist_ok=True)

                    combined_schema = {
                        "json_schema": new_class.json_schema(),
                        "ui_schema": new_class.ui_schema(),
                    }
                    with open(json_file_dir / f"{class_name}.json", "w") as f:
                        f.write(json.dumps(combined_schema, indent=4))
            else:
                model_entry = ModelMapping(
                    name=class_name,
                    mapping=full_mapping,
                )

            model_entry.value_schema = json.dumps(new_class.model_json_schema())
            if model_entry.json_schema is None:
                model_entry.json_schema = model_entry.value_schema
            self.catalogue.register_model(model_entry, model_class=new_class)

    async def _make_models_for_path(self, path_name: str):
        """Build model mappings for all Python files under a configured path."""
        path_info = path_manager.get_path(path_name)
        path = path_info["path"]
        drops = path_info.get("drops", "")
        logger.info(f"Making model_table entries for files in {path}")

        # Process each file in this path
        for file_path in path_manager.find_python_files(path_name):
            await self._create_model_models_from_file(file_path, drops)

    async def clear_table(self) -> None:
        self.logger.info("Clearing discovered model descriptors")
        self.catalogue.models.clear()


async def make_model_table(
    catalogue: CatalogueCollector,
    dirs: list[str] = None,
    drops: str = "",
    write_schema: bool = False,
    clear: bool = False,
    project_root: Path = None,
    ignore_entrypoints: bool = False,
):
    """
    Collect model descriptors from the prepared environment.
    """
    creator = CreateModelTable(catalogue, write_schema=write_schema, project_root=project_root)
    await creator.build(dirs=dirs, drops=drops, clear=clear, ignore_entrypoints=ignore_entrypoints)


def create_model_table_main():
    TableBuilderBase.cli_main(CreateModelTable)


if __name__ == "__main__":
    create_model_table_main()
