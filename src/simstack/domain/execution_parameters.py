"""Persistence-neutral inheritance of self-node allocation parameters."""

from typing import Optional
from simstack.models.parameters import Parameters, SlurmParameters

def _slurm_parameters_are_unset(slurm: Optional[SlurmParameters]) -> bool:
    """True when slurm_parameters were omitted (or are an empty default)."""
    if slurm is None:
        return True
    fields_set = getattr(slurm, "model_fields_set", None)
    if not fields_set:
        return True
    for name in fields_set:
        value = getattr(slurm, name, None)
        if value is None or value == [] or value == {}:
            continue
        return False
    return True


def inherit_parent_slurm_parameters_for_self(
    parameters: Optional[Parameters],
    parent_parameters: Optional[Parameters],
) -> Optional[Parameters]:
    """Give a self-resource child the parent's slurm_parameters when it has none."""
    if parameters is None or not isinstance(parent_parameters, Parameters):
        return parameters
    if parameters.resource != "self":
        return parameters
    if not _slurm_parameters_are_unset(getattr(parameters, "slurm_parameters", None)):
        return parameters

    parent_slurm = getattr(parent_parameters, "slurm_parameters", None)
    if not isinstance(parent_slurm, SlurmParameters):
        return parameters
    if _slurm_parameters_are_unset(parent_slurm):
        return parameters

    parameters.slurm_parameters = parent_slurm.model_copy(deep=True)
    return parameters
