import pytest

from simstack.core.resource_assignment import _merge_slurm_patch
from simstack.models.parameters import SlurmParameters


def test_extra_options_are_written_as_sbatch_lines():
    slurm = SlurmParameters(
        extra_options=["--gres=scratch:100", "--comment=pyscf"]
    )
    lines = slurm.to_sbatch_header().splitlines()
    assert "#SBATCH --gres=scratch:100" in lines
    assert "#SBATCH --comment=pyscf" in lines


def test_extra_options_reject_values_that_are_not_sbatch_flags():
    with pytest.raises(ValueError, match="starting with '--'"):
        SlurmParameters(extra_options=["gres=scratch:100"])


def test_extra_options_reject_a_flag_already_set_by_a_field():
    slurm = SlurmParameters(mem="8G", extra_options=["--mem=2G"])
    with pytest.raises(ValueError, match="repeats --mem"):
        slurm.to_sbatch_header()


def test_assignment_patch_keeps_extra_options():
    slurm = _merge_slurm_patch(
        {"mem": "8G", "extra_options": ["--gres=scratch:100"]}
    )
    lines = slurm.to_sbatch_header().splitlines()
    assert slurm.extra_options == ["--gres=scratch:100"]
    assert "#SBATCH --mem=8G" in lines
    assert "#SBATCH --gres=scratch:100" in lines
