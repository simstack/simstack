import inspect
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from simstack.models.models import ModelMapping
from simstack.models.document import DocumentParsingError
from simstack.persistence import Repository, eq
from simstack.models.simstack_model import is_simstack_model
from simstack.tables.table_builder import TableBuilderBase

logger = logging.getLogger("ModelTable")


class CreateModelTable(TableBuilderBase):
    """
    Helper class to build the model table without passing around many parameters.

    Usage:
        creator = CreateModelTable(repository)
        await creator.build()
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
            # This is required by the document-model metaclass.
            # subclass does not work even if applied to the imported classes
            # this is a bug in importlib
            bases = [base.__name__ for base in new_class.__bases__]
            is_ui_model = any("UIModel" in s for s in bases) or is_simstack_model(
                new_class
            )
            is_model = any(s == "Model" for s in bases)

            is_embedded_model = any(s == "EmbeddedModel" for s in bases)

            if not (is_model or is_ui_model or is_embedded_model):
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
                # if len(drop_modules) > 0:
                #     raise ValueError(
                #         "drop modules not empty: ", drop_modules, new_class.__module__
                #     )
            full_mapping = ".".join(new_modules) + "." + class_name
            logger.debug(f"    Class: {class_name} Model Mapping: {full_mapping}")

            # Remove any existing ModelMapping entry for this class

            try:
                existing_entries = await self.repository.find(
                    ModelMapping, eq("name", class_name)
                )
            except (ValidationError, NameError, DocumentParsingError) as e:
                logger.error(
                    f"Error finding existing ModelMapping for {class_name}: {e}"
                )
                existing_entries = []

            if len(existing_entries) > 0:
                if len(existing_entries) > 1:
                    logger.warning(
                        f"Found {len(existing_entries)} ModelMapping entries for class_name '{class_name}'. "
                        f"Expected at most one. Deleting all existing entries."
                    )
                else:
                    existing_mapping = existing_entries[0].mapping
                    if existing_mapping != full_mapping:
                        logger.warning(
                            f"Replacing ModelMapping entry for {class_name}: "
                            f"old mapping '{existing_mapping}' -> new mapping '{full_mapping}'"
                        )

                for entry in existing_entries:
                    await self.repository.delete(entry)
                    logger.debug(f"Deleted ModelMapping entry for {class_name} ({entry.mapping})")

            # EmbeddedModels have no collection by may be simstack_models. They are never saved/retrieved
            collection_name = getattr(new_class, "__collection__", None)
            if collection_name is None:
                if is_embedded_model:
                    collection_name = "EmbeddedModel"
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
                logger.debug(
                    f"SimStack Model: {class_name} Mapping: {full_mapping} Collection: {collection_name}"
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
                    collection_name=collection_name,
                    json_schema=json.dumps(new_class.model_json_schema()),
                )
                logger.debug(
                    f"Model: {class_name} Mapping: {full_mapping} Collection: {collection_name}"
                )

            await self.repository.save(model_entry)

    async def clear_table(self) -> None:
        self.logger.info("Clearing ModelMapping collection")
        await self.repository.delete_all(ModelMapping)


# Public API preserved for existing callers (e.g. tests)
async def make_model_table(
    repository: Repository,
    dirs: list[str],
    drops: str = "",
    write_schema: bool = False,
    clear: bool = False,
    project_root: Path = None,
    ignore_entrypoints: bool = False,
):
    """
    Rebuild the model table using the given repository.

    This is the application-service entry point for model-table registration.
    """
    creator = CreateModelTable(
        repository,
        write_schema=write_schema,
        project_root=project_root,
    )
    await creator.build(
        dirs=dirs, drops=drops, clear=clear, ignore_entrypoints=ignore_entrypoints
    )
