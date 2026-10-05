from types import SimpleNamespace

import pytest
from odmantic import ObjectId

from simstack.models.parameters import Resource
from simstack.util.runner_utils import SqueueNotFoundError, get_job_info, run_squeue_for_job


def _missing_squeue_result():
    return SimpleNamespace(
        returncode=127,
        stdout="",
        stderr="/bin/sh: 1: squeue: not found",
    )


def test_run_squeue_for_job_raises_when_squeue_missing(monkeypatch):
    monkeypatch.setattr(
        "simstack.util.runner_utils.subprocess.run",
        lambda *args, **kwargs: _missing_squeue_result(),
    )
    with pytest.raises(SqueueNotFoundError):
        run_squeue_for_job("12345")


def test_get_job_info_raises_when_squeue_missing(monkeypatch):
    monkeypatch.setattr(
        "simstack.util.runner_utils.subprocess.run",
        lambda *args, **kwargs: _missing_squeue_result(),
    )
    with pytest.raises(SqueueNotFoundError):
        get_job_info("12345", ObjectId(), Resource(value="self"))
