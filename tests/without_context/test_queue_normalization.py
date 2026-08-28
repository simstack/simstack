import pytest

from simstack.models.parameters import Parameters, Queue
from simstack.models.resource_assignment import ResourceAssignmentRule
from simstack.models.resource_definition import ResourceDefinition


def test_queue_enum_contains_only_canonical_execution_queues():
    assert {queue.value for queue in Queue} == {"default", "slurm-queue"}


@pytest.mark.parametrize(
    ("legacy_queue", "canonical_queue", "in_docker"),
    [
        ("cloud", "default", False),
        ("docker", "default", True),
        (" SLURM ", "slurm-queue", False),
        ("slurm_queue", "slurm-queue", False),
        ("slurm-docker", "slurm-queue", True),
        ("SLURM_DOCKER", "slurm-queue", True),
    ],
)
def test_legacy_queue_is_normalized_to_queue_and_flag(
    legacy_queue, canonical_queue, in_docker
):
    parameters = Parameters(queue=legacy_queue)

    assert parameters.queue == canonical_queue
    assert parameters.in_docker is in_docker


@pytest.mark.parametrize(
    ("legacy_queue", "canonical_queue"),
    [
        ("docker", "default"),
        ("slurm-docker", "slurm-queue"),
        ("SLURM_DOCKER", "slurm-queue"),
    ],
)
def test_legacy_queue_normalization_preserves_explicit_docker_off(
    legacy_queue, canonical_queue
):
    parameters = Parameters(queue=legacy_queue, in_docker=False)

    assert parameters.queue == canonical_queue
    assert parameters.in_docker is False


def test_legacy_queue_normalization_does_not_mutate_input_data():
    payload = {"queue": "docker", "in_docker": False}

    Parameters.model_validate(payload)

    assert payload == {"queue": "docker", "in_docker": False}


@pytest.mark.parametrize(
    ("legacy_queue", "canonical_queue"),
    [
        ("cloud", "default"),
        ("docker", "default"),
        ("slurm", "slurm-queue"),
        ("slurm_queue", "slurm-queue"),
        ("slurm-docker", "slurm-queue"),
        ("SLURM_DOCKER", "slurm-queue"),
    ],
)
def test_resource_definition_normalizes_legacy_queue(legacy_queue, canonical_queue):
    resource = ResourceDefinition(
        resource_str="local",
        workdir="/tmp/simstack",
        hostname="host",
        queue=legacy_queue,
    )

    assert resource.queue == canonical_queue


@pytest.mark.parametrize(
    ("legacy_queue", "canonical_queue", "inferred_docker"),
    [
        ("cloud", "default", None),
        ("docker", "default", True),
        ("slurm", "slurm-queue", None),
        ("slurm_queue", "slurm-queue", None),
        ("slurm-docker", "slurm-queue", True),
        ("SLURM_DOCKER", "slurm-queue", True),
    ],
)
def test_assignment_rule_normalizes_legacy_queue_and_preserves_explicit_off(
    legacy_queue, canonical_queue, inferred_docker
):
    migrated = ResourceAssignmentRule(
        name=f"legacy-{legacy_queue}",
        regex_pattern="workflow.node",
        queue=legacy_queue,
    )
    explicit_off = ResourceAssignmentRule(
        name=f"legacy-{legacy_queue}-off",
        regex_pattern="workflow.node",
        queue=legacy_queue,
        in_docker=False,
    )

    assert migrated.queue == canonical_queue
    assert migrated.in_docker is inferred_docker
    assert explicit_off.queue == canonical_queue
    assert explicit_off.in_docker is False
