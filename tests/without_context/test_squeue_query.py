import subprocess
from types import SimpleNamespace

import pytest
from odmantic import ObjectId

from simstack.models.parameters import Resource
from simstack.util.runner_utils import SqueueQueryError, get_job_info

SQUEUE_HEADER = "JOBID PARTITION NAME USER ST TIME NODES NODELIST(REASON)"
SQUEUE_JOB_LINE = "12345 batch hyperpol user R 1:23 1 node01"


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_squeue(monkeypatch, result):
    monkeypatch.setattr(
        "simstack.util.runner_utils.subprocess.run",
        lambda *args, **kwargs: result,
    )


def test_get_job_info_parses_running_job(monkeypatch):
    _patch_squeue(monkeypatch, _completed(f"{SQUEUE_HEADER}\n{SQUEUE_JOB_LINE}\n"))
    info = get_job_info("12345", ObjectId(), Resource(value="self"))
    assert info is not None
    assert info.job_id == "12345"
    assert info.name == "hyperpol"
    assert info.code == "R"
    assert info.time == "1:23"
    assert info.nodes == ["node01"]


def test_get_job_info_returns_none_when_job_left_the_queue(monkeypatch):
    _patch_squeue(monkeypatch, _completed(f"{SQUEUE_HEADER}\n"))
    assert get_job_info("12345", ObjectId(), Resource(value="self")) is None


def test_get_job_info_raises_when_squeue_times_out(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="squeue -j 12345", timeout=30)

    monkeypatch.setattr("simstack.util.runner_utils.subprocess.run", raise_timeout)
    with pytest.raises(SqueueQueryError, match="timed out"):
        get_job_info("12345", ObjectId(), Resource(value="self"))


def test_get_job_info_raises_when_squeue_returns_nonzero(monkeypatch):
    _patch_squeue(
        monkeypatch,
        _completed(
            "",
            returncode=1,
            stderr="slurm_load_jobs error: Socket timed out on send/recv operation",
        ),
    )
    with pytest.raises(SqueueQueryError, match="return code 1"):
        get_job_info("12345", ObjectId(), Resource(value="self"))


def test_get_job_info_raises_when_squeue_returns_empty_stdout(monkeypatch):
    _patch_squeue(monkeypatch, _completed("   \n", returncode=0))
    with pytest.raises(SqueueQueryError, match="returned no output"):
        get_job_info("12345", ObjectId(), Resource(value="self"))


def test_get_job_info_raises_when_squeue_output_is_not_a_listing(monkeypatch):
    _patch_squeue(
        monkeypatch,
        _completed("slurm_load_jobs error: Unable to contact slurm controller\n"),
    )
    with pytest.raises(SqueueQueryError, match="unexpected output"):
        get_job_info("12345", ObjectId(), Resource(value="self"))
