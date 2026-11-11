"""Read Slurm evidence without deciding persisted task state."""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone

ACTIVE_STATES = {
    "PENDING",
    "RUNNING",
    "CONFIGURING",
    "COMPLETING",
    "RESIZING",
    "REQUEUED",
    "REQUEUE_FED",
    "REQUEUE_HOLD",
    "SPECIAL_EXIT",
    "RESV_DEL_HOLD",
    "SUSPENDED",
    "SIGNALING",
    "STAGE_OUT",
    "STOPPED",
}
TERMINAL_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "TIMEOUT",
}
QUERY_ERRORS = (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError)


def query_environment():
    """Keep scheduler output independent of user display/filter settings."""
    return {**{name: value for name, value in os.environ.items()
               if not name.startswith("SQUEUE_")},
            "TZ": "UTC", "SLURM_TIME_FORMAT": "%Y-%m-%dT%H:%M:%S"}


def _run(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=query_environment(),
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _state(value):
    parts = value.strip().split()
    return parts[0].rstrip("+").upper() if parts else ""


def observe_job(job_id, *, restart_count=None):
    if not re.fullmatch(r"\d+(?:_\d+)?", str(job_id)):
        raise ValueError("Invalid Slurm job identity")
    observed = {
        "job_id": str(job_id),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    errors, queue_known = [], False
    array_base = "_" not in str(job_id)

    def matches(identity):
        return identity == str(job_id) or array_base and identity.partition("_")[0] == str(job_id)

    try:
        output = _run(
            [
                "squeue",
                "--noheader",
                "--states=all",
                "--format=" + ("%F" if array_base else "%i") + "|%T|%t|%j|%u|%M|%N|%R",
            ]
        )
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = [part.strip() for part in line.split("|", 7)]
            if len(fields) != 8:
                raise ValueError("Unexpected scheduler queue response")
            if not matches(fields[0]):
                continue
            state = _state(fields[1])
            if state in ACTIVE_STATES:
                return {
                    **observed,
                    "kind": "active",
                    "state": state,
                    "code": fields[2],
                    "job_name": fields[3],
                    "user": fields[4],
                    "elapsed": fields[5],
                    "nodes": fields[6],
                    "reason": fields[7],
                    "source": "squeue",
                }
            if state not in TERMINAL_STATES:
                raise ValueError("Unexpected scheduler queue state")
        queue_known = True
    except QUERY_ERRORS as exc:
        errors.append(str(exc))
    try:
        output = _run(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--allocations",
                "--array",
                "--jobs",
                str(job_id),
                "--format=JobID,State,ExitCode,End,Reason" + (",Restarts" if restart_count is not None else ""),
            ]
        )
        facts = []
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split("|")
            if len(fields) != (6 if restart_count is not None else 5):
                raise ValueError("Unexpected scheduler accounting response")
            if not matches(fields[0].strip()):
                continue
            if not re.fullmatch(r"\d+(?:_\d+)?", fields[0].strip()):
                raise ValueError("Unexpected scheduler accounting response")
            reported_restart = int(fields[5]) if restart_count is not None else None
            if restart_count is not None and reported_restart < restart_count:
                continue
            state = _state(fields[1])
            if state not in ACTIVE_STATES | TERMINAL_STATES:
                raise ValueError("Unexpected scheduler accounting state")
            code, signal = fields[2].split(":", 1)
            ended_at = fields[3] if fields[3] not in {"", "Unknown", "None"} else None
            if ended_at:
                ended_at = (
                    datetime.fromisoformat(ended_at)
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                )
            facts.append({
                "identity": fields[0].strip(),
                "kind": "terminal" if state in TERMINAL_STATES else "active",
                "state": state,
                "exit_code": int(code),
                "signal": int(signal),
                "ended_at": ended_at,
                "reason": fields[4],
                "source": "sacct",
                **({"restart_count": reported_restart} if restart_count is not None else {}),
            })
        if facts and restart_count is not None:
            # A requeued script can fail during bootstrap before Server admits
            # its new incarnation. Its terminal accounting still proves the
            # submitted allocation stopped. Prefer its latest scheduler run.
            latest = max(fact["restart_count"] for fact in facts)
            facts = [fact for fact in facts if fact["restart_count"] == latest]
        active = next((fact for fact in facts if fact["kind"] == "active"), None)
        if active is not None:
            return {**observed, **{key: value for key, value in active.items() if key != "identity"}}
        is_array = array_base and any("_" in fact["identity"] for fact in facts)
        # Accounting may lag behind unobserved array members. A failed queue
        # query cannot prove that the terminal rows cover the whole allocation.
        if facts and (not is_array or queue_known):
            terminal = next((fact for fact in facts if fact["state"] != "COMPLETED"), facts[0])
            result = {**observed, **{key: value for key, value in terminal.items() if key != "identity"}}
            if is_array:
                result["ended_at"] = max((fact["ended_at"] for fact in facts if fact["ended_at"]), default=None)
                counts = Counter(fact["state"] for fact in facts)
                result["reason"] = ("Array tasks: " + ", ".join(f"{state}={count}" for state, count in sorted(counts.items()))
                    + f"; {terminal['identity']}: {terminal['reason']}")
            return result
    except QUERY_ERRORS as exc:
        errors.append(str(exc))
    reason = (
        "; ".join(errors)
        if errors
        else "Job absent from queue; accounting has no conclusive outcome"
    )
    return {
        **observed,
        "kind": "unknown",
        "job_absent": queue_known and not errors,
        "reason": reason[:500],
        "source": "scheduler",
    }


def find_submission(submission_id, submitted_at):
    """Resolve an ambiguous external submission; this function never submits."""
    from uuid import UUID

    name = "ss-" + str(UUID(submission_id))
    start = (
        submitted_at
        if isinstance(submitted_at, datetime)
        else datetime.fromisoformat(submitted_at)
    )
    start = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    errors, jobs = [], set()

    def identities(output, *, accounting=False):
        result = set()
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = [value.strip() for value in line.split("|")]
            if (
                len(fields) < 2
                or (not accounting and len(fields) != 2)
                or not re.fullmatch(r"\d+(?:_\d+)?", fields[0])
            ):
                raise ValueError("Unexpected scheduler submission lookup response")
            if fields[1] == name:
                result.add(fields[0].partition("_")[0])
        return result

    try:
        output = _run(["squeue", "--noheader", "--name", name, "--states=all", "--format=%F|%j"])
        jobs = identities(output)
    except QUERY_ERRORS as exc:
        errors.append(str(exc))
    if not jobs:
        try:
            output = _run(
                [
                    "sacct",
                    "--noheader",
                    "--parsable2",
                    "--allocations",
                    "--array",
                    "--name",
                    name,
                    "--starttime",
                    start,
                    "--format=JobID,JobName%100",
                ]
            )
            jobs = identities(output, accounting=True)
        except QUERY_ERRORS as exc:
            errors.append(str(exc))
    if len(jobs) > 1:
        raise RuntimeError("Multiple Slurm jobs match one submission identity")
    if not jobs and errors:
        raise RuntimeError("; ".join(errors))
    return next(iter(jobs), None)
