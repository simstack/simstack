"""Operational runner controls survive process isolation; database settings do not."""

import pytest

from simstack.core.runner_execution.config import child_poll_seconds
from simstack.core.runner_execution.errors import RunnerTaskExecutionError
from simstack.runner_runtime.environment import (
    RUNNER_CONTROL_ENVIRONMENT,
    RUNNER_RUNTIME_ENVIRONMENT,
    sanitized_runner_environment,
    sanitized_workflow_subprocess_environment,
    sanitize_runner_process_environment,
    sanitize_worker_process_environment,
    slurm_export_argument,
)


def test_operational_controls_survive_supervisor_worker_and_scheduler_boundaries():
    settings = {name: "15" for name in RUNNER_RUNTIME_ENVIRONMENT}
    source = {
        **settings,
        "CONNECTION_STRING": "forbidden",
        "SIMSTACK_FILE_TRANSFER_MAX_BYTES": "server-owned",
    }
    supervisor = dict(source)
    sanitize_runner_process_environment(environment=supervisor)
    assert supervisor == settings
    controls = {name: "test-control" for name in RUNNER_CONTROL_ENVIRONMENT}
    worker = sanitized_runner_environment(controls, source=supervisor)
    sanitize_worker_process_environment(environment=worker)
    assert all(worker[name] == value for name, value in settings.items())
    exported = slurm_export_argument(worker).removeprefix("--export=").split(",")
    assert set(settings) <= set(exported)
    assert "CONNECTION_STRING" not in worker
    assert "SIMSTACK_FILE_TRANSFER_MAX_BYTES" not in worker
    assert sanitized_workflow_subprocess_environment((), source=worker) == {}


def test_slurm_exports_only_approved_apptainer_environment_counterparts():
    controls = {name: "test-control" for name in RUNNER_CONTROL_ENVIRONMENT}
    environment = {
        **controls,
        "ProgramLicense": "allowed-license",
        **{f"APPTAINERENV_{name}": value for name, value in controls.items()},
        "APPTAINERENV_ProgramLicense": "allowed-license",
        "APPTAINERENV_DATABASE_URL": "forbidden",
        "APPTAINERENV_UNDECLARED_SECRET": "forbidden",
    }
    exported = slurm_export_argument(
        environment, extra_names=("ProgramLicense",),
    ).removeprefix("--export=").split(",")
    assert {f"APPTAINERENV_{name}" for name in controls} <= set(exported)
    assert "APPTAINERENV_ProgramLicense" in exported
    assert "APPTAINERENV_DATABASE_URL" not in exported
    assert "APPTAINERENV_UNDECLARED_SECRET" not in exported
    assert len(exported) == len(set(exported))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "invalid"])
def test_child_poll_interval_must_be_finite_and_positive(monkeypatch, value):
    monkeypatch.setenv("SIMSTACK_RUNNER_CHILD_POLL_SECONDS", value)
    with pytest.raises(RunnerTaskExecutionError):
        child_poll_seconds()
