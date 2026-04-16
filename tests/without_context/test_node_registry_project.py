from simstack.core.definitions import TaskStatus
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters


def _build_node_registry(**overrides) -> NodeRegistry:
    values = {
        "name": "test-node",
        "status": TaskStatus.SUBMITTED,
        "input_ids": [],
        "input_tables": [],
        "function_hash": "function-hash",
        "arg_hash": "arg-hash",
        "func_mapping": "tests.module.function",
        "parameters": Parameters(),
    }
    values.update(overrides)
    return NodeRegistry(**values)


def test_reassigning_parameters_preserves_project_field():
    node_registry = _build_node_registry()

    node_registry.parameters = Parameters(queue="slurm-queue")

    assert node_registry.project == "default"
    assert node_registry.model_dump_doc()["project"] == "default"


def test_legacy_new_project_payload_maps_to_project():
    node_registry = _build_node_registry(
        project=None,
        new_project={"field_name": "legacy-project"},
    )

    assert node_registry.project == "legacy-project"
