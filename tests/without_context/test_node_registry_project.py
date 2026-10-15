from simstack.runner_protocol.constants import RunnerTaskStatus as TaskStatus
from simstack.models.document import ObjectId
from simstack.models.node_registry import NodeRegistry
from simstack.models.parameters import Parameters

import logging
logger = logging.getLogger(__name__)

def _build_node_registry(**overrides) -> NodeRegistry:
    values = {
        "name": "test-node",
        "status": TaskStatus.SUBMITTED,
        "input_references": [],
        "function_hash": "function-hash",
        "arg_hash": "arg-hash",
        "func_mapping": "tests.module.function",
        "parameters": Parameters(),
    }
    values.update(overrides)

    return NodeRegistry(**values)


def test_reassigning_parameters_preserves_project_field():
    logger.info("test_reassigning_parameters_preserves_project_field")
    project_id = ObjectId()
    node_registry = _build_node_registry(project=project_id)

    node_registry.parameters = Parameters(queue="slurm-queue")

    assert node_registry.project == project_id
    assert node_registry.model_dump_doc()["project"] == project_id


def test_reassigning_parameters_keeps_project_none_when_not_set():
    node_registry = _build_node_registry()

    node_registry.parameters = Parameters(queue="slurm-queue")

    assert node_registry.project is None
    assert node_registry.model_dump_doc()["project"] is None


def test_parameters_remain_an_embedded_document_not_a_reference():
    node_registry = _build_node_registry()

    parameters_schema = NodeRegistry.model_json_schema()["properties"]["parameters"]
    dumped_parameters = node_registry.model_dump_doc()["parameters"]

    assert parameters_schema == {"$ref": "#/$defs/Parameters"}
    assert isinstance(dumped_parameters, dict)
    assert dumped_parameters["queue"] == "default"
