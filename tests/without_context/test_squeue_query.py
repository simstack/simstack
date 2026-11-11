import subprocess
from types import SimpleNamespace
import pytest
from simstack.util.slurm_observation import observe_job

def test_queue_probe_has_bounded_argv_and_preserves_job_identity(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="999|PENDING|PD|other-job|user|0:00|node02|Resources\n12345|RUNNING|R|orca-job|artem|1:23|node01|None\n", stderr="")
    monkeypatch.setattr("simstack.util.slurm_observation.subprocess.run", run)
    result = observe_job("12345")
    assert result["kind"] == "active"
    assert result["state"] == "RUNNING"
    assert result["job_id"] == "12345"
    assert result["job_name"] == "orca-job"
    assert result["elapsed"] == "1:23"
    # An all-jobs view can establish absence without a missing-ID query error;
    # the observer must still select only the requested job from that view.
    assert calls[0][0] == ["squeue", "--noheader", "--states=all", "--format=%F|%T|%t|%j|%u|%M|%N|%R"]
    assert calls[0][1]["timeout"] == 30
    assert calls[0][1]["env"]["TZ"] == "UTC"
    assert "shell" not in calls[0][1]

@pytest.mark.parametrize("error", [
    FileNotFoundError("squeue not installed"),
    subprocess.TimeoutExpired("squeue", 30),
])
def test_query_failures_are_unknown_evidence(monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr("simstack.util.slurm_observation.subprocess.run", run)
    assert observe_job("12345")["kind"] == "unknown"

@pytest.mark.parametrize("stdout,returncode,stderr", [
    ("", 1, "Socket timed out on send/recv operation"),
    ("", 127, "squeue not found"),
    ("unexpected controller output", 0, ""),
    ("", 0, ""),
])
def test_absence_bad_output_and_nonzero_never_invent_timeout(monkeypatch, stdout, returncode, stderr):
    monkeypatch.setattr("simstack.util.slurm_observation.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
    assert observe_job("12345")["kind"] == "unknown"

def test_invalid_scheduler_job_id_is_not_interpreted_as_a_shell_command():
    with pytest.raises(ValueError, match="job identity"):
        observe_job("42; touch /tmp/should-not-exist")


def test_scheduler_display_filters_do_not_change_observation_or_connection_environment(monkeypatch):
    filters = ["SQUEUE_STATES", "SQUEUE_USERS", "SQUEUE_PARTITION", "SQUEUE_ACCOUNT", "SQUEUE_NAMES", "SQUEUE_QOS", "SQUEUE_FORMAT2"]
    for name in filters:
        monkeypatch.setenv(name, "caller-display-filter")
    monkeypatch.setenv("SLURM_CONF", "/cluster/slurm.conf")
    monkeypatch.setenv("SLURM_CLUSTERS", "compute")

    def run(command, **kwargs):
        assert not any(name.startswith("SQUEUE_") for name in kwargs["env"])
        assert kwargs["env"]["SLURM_CONF"] == "/cluster/slurm.conf"
        assert kwargs["env"]["SLURM_CLUSTERS"] == "compute"
        return SimpleNamespace(returncode=0, stdout="42|PENDING|PD|array|user|0:00|node01|Resources\n", stderr="")

    monkeypatch.setattr("simstack.util.slurm_observation.subprocess.run", run)
    assert observe_job("42")["kind"] == "active"
