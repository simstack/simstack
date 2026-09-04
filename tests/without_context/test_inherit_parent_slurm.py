from simstack.core.node import Node, inherit_parent_slurm_parameters_for_self
from simstack.core.resource_assignment import empty_slurm_parameters
from simstack.models.parameters import Parameters, SlurmParameters


def _dummy_node_func(data, **kwargs):
    return data


def test_self_resource_inherits_parent_slurm_when_unset():
    child = Parameters(resource="self")
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, tasks=2, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 4
    assert child.slurm_parameters.tasks == 2
    assert child.slurm_parameters.mem == "8G"
    assert child.slurm_parameters is not parent.slurm_parameters


def test_self_resource_keeps_explicit_child_slurm():
    child = Parameters(
        resource="self",
        slurm_parameters=SlurmParameters(cpus_per_task=1, mem="2G"),
    )
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 1
    assert child.slurm_parameters.mem == "2G"


def test_non_self_resource_does_not_inherit_parent_slurm():
    child = Parameters(resource="local")
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 1
    assert child.slurm_parameters.mem == "1G"


def test_cleared_slurm_on_self_still_inherits_parent():
    child = Parameters(resource="self")
    child.slurm_parameters = empty_slurm_parameters()
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 4
    assert child.slurm_parameters.mem == "8G"


def test_execute_path_fills_self_slurm_before_forwarding_parent_parameters():
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, tasks=2, mem="8G"),
    )
    node = Node(
        func=_dummy_node_func,
        is_async=False,
        parameters=Parameters(resource="self"),
        parent_parameters=parent,
    )

    assert node._apply_parent_slurm_for_self_resource() is True
    assert node.parameters.slurm_parameters.cpus_per_task == 4
    assert node.parameters.slurm_parameters.mem == "8G"



def test_self_resource_inherits_parent_slurm_when_unset():
    child = Parameters(resource="self")
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, tasks=2, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 4
    assert child.slurm_parameters.tasks == 2
    assert child.slurm_parameters.mem == "8G"
    assert child.slurm_parameters is not parent.slurm_parameters


def test_self_resource_keeps_explicit_child_slurm():
    child = Parameters(
        resource="self",
        slurm_parameters=SlurmParameters(cpus_per_task=1, mem="2G"),
    )
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 1
    assert child.slurm_parameters.mem == "2G"


def test_non_self_resource_does_not_inherit_parent_slurm():
    child = Parameters(resource="local")
    parent = Parameters(
        resource="cloud",
        slurm_parameters=SlurmParameters(cpus_per_task=4, mem="8G"),
    )

    inherit_parent_slurm_parameters_for_self(child, parent)

    assert child.slurm_parameters.cpus_per_task == 1
    assert child.slurm_parameters.mem == "1G"
