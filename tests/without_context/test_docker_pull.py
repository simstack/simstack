from simstack.core.run_docker import resolve_docker_pull_ref
from simstack.util.resource_config import ResourceConfig


def test_resolve_skips_local_hub_library_without_registry():
    assert resolve_docker_pull_ref("docker.io/library/molecular-qm-psi4:latest", None) is None
    assert resolve_docker_pull_ref("molecular-qm-psi4:latest", None) is None


def test_resolve_rewrites_hub_library_via_docker_registry():
    assert (
        resolve_docker_pull_ref(
            "docker.io/library/molecular-qm-psi4:latest",
            "167.233.117.31:5000",
        )
        == "167.233.117.31:5000/molecular-qm-psi4:latest"
    )


def test_resolve_keeps_explicit_registry_ref():
    ref = "167.233.117.31:5000/molecular-qm-psi4:latest"
    assert resolve_docker_pull_ref(ref, None) == ref
    assert resolve_docker_pull_ref(ref, "example.com:5000") == ref


def test_resolve_skips_sif_and_apptainer_docker_uri():
    assert resolve_docker_pull_ref("/shared/software/simstack2/containers/molecular_qm_psi4.sif", "167.233.117.31:5000") is None
    assert resolve_docker_pull_ref("docker://molecular-qm-psi4:latest", "167.233.117.31:5000") is None


def test_resource_config_docker_registry(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[big]\ndocker_registry = "167.233.117.31:5000"\n'
        "[local.program.orca]\nrun_command = \"orca\"\n"
    )
    big = ResourceConfig(tmp_path, "big")
    assert big.get_docker_registry() == "167.233.117.31:5000"
    local = ResourceConfig(tmp_path, "local")
    assert local.get_docker_registry() is None
    assert local.get_docker_registry("big") == "167.233.117.31:5000"
