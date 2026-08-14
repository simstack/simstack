from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odmantic import ObjectId

from simstack.core.context import context
from simstack.core.generated_workflow import (
    generated_module_path,
    import_materialized_generated_module,
    materialize_generated_workflow_source,
)
from simstack.core.generated_workflow_validation import (
    validate_generated_workflow_source,
)
from simstack.core.node import node
from simstack.models.generated_workflow import (
    GeneratedWorkflowSource,
    GeneratedWorkflowStatus,
)
from simstack.models.models import ModelMapping, NodeModel
from simstack.models.parameters import Parameters, Resource
from simstack.models.resource_definition import ResourceDefinition
from simstack.models.simstack_model import is_simstack_model
from simstack.tables.model_table import make_model_table
from simstack.tables.node_table import make_node_table


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _save_source_state(
    source: GeneratedWorkflowSource,
    status: GeneratedWorkflowStatus,
    *,
    error: str | None = None,
) -> None:
    source.status = status
    source.error = error
    source.updated_at = _utc_now()
    await context.db.save(source)


def _generated_node_names(module: Any) -> set[str]:
    return {
        getattr(func, "_node_name", func_name)
        for func_name, func in inspect.getmembers(module, inspect.isfunction)
        if func.__module__ == module.__name__
        and getattr(func, "_is_node", False) is True
    }


def _generated_model_names(module: Any) -> set[str]:
    names: set[str] = set()
    for class_name, model_class in inspect.getmembers(module, inspect.isclass):
        if model_class.__module__ != module.__name__:
            continue
        base_names = {base.__name__ for base in model_class.__bases__}
        if (
            "Model" in base_names
            or "EmbeddedModel" in base_names
            or any("UIModel" in name for name in base_names)
            or is_simstack_model(model_class)
        ):
            names.add(class_name)
    return names


async def _registered_name_belongs_to_workflow(
    source_revision: ObjectId | None,
    workflow_id: str,
) -> bool:
    if source_revision is None:
        return False
    owner = await context.db.find_one(
        GeneratedWorkflowSource,
        GeneratedWorkflowSource.id == source_revision,
    )
    return owner is not None and owner.workflow_id == workflow_id


async def _ensure_registration_names_available(
    source: GeneratedWorkflowSource,
    module: Any,
) -> None:
    """Prevent one generated workflow from replacing another's table mappings."""

    for model_type, field, names, kind in (
        (NodeModel, NodeModel.name, _generated_node_names(module), "node"),
        (ModelMapping, ModelMapping.name, _generated_model_names(module), "model"),
    ):
        for name in sorted(names):
            existing = await context.db.find_one(model_type, field == name)
            if existing is None:
                continue
            if await _registered_name_belongs_to_workflow(
                existing.source_revision,
                source.workflow_id,
            ):
                continue
            raise ValueError(
                f"generated {kind} name '{name}' is already registered by "
                "another workflow"
            )


@node(
    parameters=Parameters(),
    expose_in_submit=False,
)
async def install_generated_workflow(
    source: GeneratedWorkflowSource,
    **kwargs: Any,
) -> bool:
    """Install and register one immutable generated workflow on this runner."""

    task_id = kwargs.get("task_id")
    if task_id is not None:
        source.install_task_id = (
            task_id if isinstance(task_id, ObjectId) else ObjectId(str(task_id))
        )
    await _save_source_state(source, GeneratedWorkflowStatus.INSTALLING)

    try:
        current_resource = str(context.config.resource)
        if source.target_resource != current_resource:
            raise ValueError(
                f"source targets resource '{source.target_resource}', "
                f"but installer is running on '{current_resource}'"
            )

        # Persisted source is untrusted input. Re-check it on the runner before
        # writing or importing the module, even if the server already validated it.
        validate_generated_workflow_source(source.source_code)

        materialized = materialize_generated_workflow_source(source)
        module = import_materialized_generated_module(source)
        await _ensure_registration_names_available(source, module)

        # These are ordinary table builds scoped to the one exact .py file.
        # They intentionally preserve every unrelated mapping collection entry.
        await make_model_table(
            context.db,
            dirs=[materialized.file_path],
            drops="",
            clear=False,
            project_root=materialized.root,
            ignore_entrypoints=True,
        )
        await make_node_table(
            context.db,
            dirs=[materialized.file_path],
            drops="",
            clear=False,
            project_root=materialized.root,
            ignore_entrypoints=True,
        )

        resource_definition = await context.db.find_one(
            ResourceDefinition,
            ResourceDefinition.resource_str == source.target_resource,
        )
        if resource_definition is None:
            raise ValueError(
                f"resource definition '{source.target_resource}' was not found"
            )

        generated_nodes = await context.db.find(
            NodeModel,
            (NodeModel.source_revision == source.id)
            & (NodeModel.source_sha256 == source.source_sha256),
        )
        missing_input_mappings: list[str] = []
        for generated_node in generated_nodes:
            for input_mapping in generated_node.input_mappings:
                registered_model = await context.db.find_one(
                    ModelMapping,
                    ModelMapping.mapping == input_mapping.mapping,
                )
                if registered_model is None:
                    missing_input_mappings.append(
                        f"{generated_node.name}.{input_mapping.name} -> "
                        f"{input_mapping.mapping}"
                    )
        if missing_input_mappings:
            raise ValueError(
                "generated node inputs reference unregistered ModelMapping "
                "entries: " + ", ".join(sorted(missing_input_mappings))
            )
        for generated_node in generated_nodes:
            generated_node.default_parameters.resource = Resource(
                value=source.target_resource
            )
            generated_node.default_parameters.queue = resource_definition.queue
            await context.db.save(generated_node)
        await context.refresh_mappings(models=False)

        module_path = generated_module_path(source)
        entrypoint_mapping = f"{module_path}.{source.entrypoint_name}"
        entrypoint = getattr(module, source.entrypoint_name, None)
        entrypoint_inner = getattr(entrypoint, "_inner", entrypoint)
        if (
            entrypoint is None
            or not inspect.isfunction(entrypoint)
            or not getattr(entrypoint, "_is_node", False)
            or not getattr(entrypoint, "_node_expose_in_submit", True)
            or entrypoint.__module__ != module_path
        ):
            raise ValueError(
                f"entrypoint '{entrypoint_mapping}' is not an exposed SimStack node"
            )

        entrypoint_file = inspect.getsourcefile(entrypoint_inner)
        if (
            entrypoint_file is None
            or Path(entrypoint_file).resolve() != materialized.file_path
        ):
            raise ValueError(
                "entrypoint was not loaded from the exact materialized file"
            )

        node_model = await context.db.find_one(
            NodeModel,
            NodeModel.function_mapping == entrypoint_mapping,
        )
        if (
            node_model is None
            or node_model.source_revision != source.id
            or node_model.source_sha256 != source.source_sha256
        ):
            raise ValueError(
                f"entrypoint '{entrypoint_mapping}' was not registered with its source pin"
            )

        await _save_source_state(source, GeneratedWorkflowStatus.READY)
        return True
    except Exception as exc:
        await _save_source_state(
            source,
            GeneratedWorkflowStatus.FAILED,
            error=str(exc),
        )
        return False
