from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from simstack.core.programs import invocation_argv, run_program
from simstack.models.files import FileStack
from simstack.runner_protocol.dto.programs import (
    NamedProgramPlanV1,
    RunnerProgramPlanResponseV1,
)
from simstack.runner_runtime.execution_adapter import RunnerExecutionAdapterConfig
from simstack.runner_runtime.programs import named_program_policy


def test_projection_contains_only_explicit_program_plan():
    adapter = RunnerExecutionAdapterConfig.from_mapping(
        {
            "r": {
                "program": {
                    "python": {"local_worker_command": ["python"]},
                    "orca": {
                        "run_command": ["orca", "orca.inp"],
                        "scripts": [],
                        "secret": "never-export",
                    },
                },
                "connection_string": "never-export",
            }
        },
        resource="r",
    )
    policy = named_program_policy(adapter, claim_id="claim", attempt=1)
    assert set(policy.programs) == {"orca"}
    assert "never-export" not in policy.model_dump_json()
    assert policy.programs["orca"].command == ["orca", "orca.inp"]


@pytest.mark.parametrize(
    "variable", ["SIMSTACK_RUNNER_TOKEN", "MONGODB_URI", "CONNECTION_STRING"]
)
def test_projection_rejects_control_environment(variable):
    adapter = RunnerExecutionAdapterConfig.from_mapping(
        {
            "r": {
                "program": {
                    "orca": {
                        "run_command": ["orca"],
                        "worker_environment_allowlist": [variable],
                    },
                }
            }
        },
        resource="r",
    )
    with pytest.raises(RuntimeError):
        named_program_policy(adapter, claim_id="c", attempt=1)


def test_arguments_never_become_script_source(tmp_path):
    value = '$(touch should-not-exist); "quoted"'
    plan = NamedProgramPlanV1(command='printf "%s" "$1"', scripts=["true"])
    process = subprocess.run(
        invocation_argv(plan, [value]), cwd=tmp_path, capture_output=True, check=True
    )
    assert process.stdout.decode() == value
    assert not (tmp_path / "should-not-exist").exists()


def test_argv_command_with_setup_preserves_arguments(tmp_path):
    value = "two words; not shell"
    plan = NamedProgramPlanV1(
        command=[sys.executable, "-c", "import sys; print(sys.argv[1])"],
        scripts=["true"],
    )
    process = subprocess.run(
        invocation_argv(plan, [value]), cwd=tmp_path, capture_output=True, check=True
    )
    assert process.stdout.decode().strip() == value


def test_program_policy_cannot_accept_arbitrary_extra_fields():
    with pytest.raises(ValidationError):
        NamedProgramPlanV1(command=["true"], cwd="/outside")


@pytest.mark.asyncio
async def test_real_program_uses_current_allocation_and_durable_files(
    monkeypatch, tmp_path
):
    import simstack.core.context as context_module
    import simstack.core.runner_execution.blobs as blobs_module
    from simstack.core.runner_execution.state import (
        RunnerClaimContext,
        activate_runner_claim,
        reset_runner_claim,
    )
    from simstack.models.parameters import Resource

    calls, published = [], []

    class Api:
        async def get_program_plan(self, task_id, *, claim_id, attempt, program):
            calls.append((task_id, claim_id, attempt, program))
            return RunnerProgramPlanResponseV1(
                task_id=task_id,
                claim_id=claim_id,
                attempt=attempt,
                program=program,
                policy_revision="a" * 64,
                plan=NamedProgramPlanV1(
                    command=[
                        sys.executable,
                        "-c",
                        "import os,pathlib; assert 'SIMSTACK_RUNNER_TOKEN' not in os.environ; pathlib.Path('answer.txt').write_text(pathlib.Path('input.txt').read_text()); print('ok')",
                    ],
                    timeout_seconds=10,
                ),
            )

    fake = SimpleNamespace(
        runner_api=Api(),
        runner_mode=True,
        initialized=True,
        worker_mode=True,
        config=SimpleNamespace(
            workdir=tmp_path,
            resource=Resource(value="r"),
            max_inline_document_bytes=1024,
        ),
    )
    monkeypatch.setattr(context_module, "context", fake)
    monkeypatch.setattr(
        blobs_module, "publish_runtime_blobs", lambda values: published.extend(values)
    )
    monkeypatch.setenv("SIMSTACK_RUNNER_TOKEN", "must-not-reach-program")
    token = activate_runner_claim(RunnerClaimContext("a" * 24, "claim", 2))
    try:
        result = await run_program(
            "answer",
            input_files=[FileStack.from_string("42", "input.txt")],
            output_names=["answer.txt"],
        )
    finally:
        reset_runner_claim(token)
    assert result.returncode == 0
    assert [value.name for value in result.output_files] == ["answer.txt"]
    assert result.output_files[0].size == 2
    assert len(published) == 3
    assert calls == [("a" * 24, "claim", 2, "answer")] * 2
    result_path = next(
        (tmp_path / "tasks" / ("a" * 24) / "programs").glob("*/answer.txt")
    )
    assert result_path.read_text() == "42"


@pytest.mark.asyncio
async def test_program_requires_an_active_api_claim():
    from simstack.core.runner_execution.errors import RunnerTaskExecutionError

    with pytest.raises(RunnerTaskExecutionError):
        await run_program("orca")


def test_plain_string_command_forwards_arguments_without_shell(tmp_path):
    value = "$(touch wrong); spaced argument"
    plan = NamedProgramPlanV1(command='printf "%s"')
    result = subprocess.run(
        invocation_argv(plan, [value]), cwd=tmp_path, capture_output=True, check=True
    )
    assert result.stdout.decode() == value
    assert not (tmp_path / "wrong").exists()


def test_shell_command_requires_explicit_argument_contract():
    plan = NamedProgramPlanV1(command="orca orca.inp > orca.out")
    assert invocation_argv(plan, [])[-1] == "simstack-program"
    with pytest.raises(ValueError, match="explicitly"):
        invocation_argv(plan, ["more.inp"])


@pytest.mark.asyncio
async def test_program_timeout_stops_descendants_and_preserves_worker(
    monkeypatch, tmp_path
):
    import simstack.core.context as context_module
    from simstack.core.runner_execution.state import (
        RunnerClaimContext,
        activate_runner_claim,
        reset_runner_claim,
    )
    from simstack.models.parameters import Resource

    script = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],process_group=0); pathlib.Path('descendant.pid').write_text(str(p.pid)); time.sleep(60)"

    class Api:
        async def get_program_plan(self, task_id, *, claim_id, attempt, program):
            return RunnerProgramPlanResponseV1(
                task_id=task_id,
                claim_id=claim_id,
                attempt=attempt,
                program=program,
                policy_revision="b" * 64,
                plan=NamedProgramPlanV1(
                    command=[sys.executable, "-c", script], timeout_seconds=1
                ),
            )

    fake = SimpleNamespace(
        runner_api=Api(),
        runner_mode=True,
        config=SimpleNamespace(workdir=tmp_path, resource=Resource(value="r")),
    )
    monkeypatch.setattr(context_module, "context", fake)
    token = activate_runner_claim(RunnerClaimContext("b" * 24, "claim", 1))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await run_program("sleep")
    finally:
        reset_runner_claim(token)
    pid = int(
        next(
            (tmp_path / "tasks" / ("b" * 24) / "programs").glob("*/descendant.pid")
        ).read_text()
    )
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().rpartition(") ")[2].split()[0] == "Z"
    (tmp_path / "worker-continued").write_text("yes")


@pytest.mark.asyncio
async def test_worker_session_teardown_includes_native_subgroups(tmp_path):
    from simstack.runner_runtime.processes import terminate_worker_tree

    script = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],process_group=0); pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, cwd=tmp_path, start_new_session=True
    )
    try:
        for _ in range(100):
            if (tmp_path / "child.pid").exists():
                break
            await asyncio.sleep(0.01)
        pid = int((tmp_path / "child.pid").read_text())
        await terminate_worker_tree(worker)
        stat = Path(f"/proc/{pid}/stat")
        assert (
            not stat.exists() or stat.read_text().rpartition(") ")[2].split()[0] == "Z"
        )
    finally:
        await terminate_worker_tree(worker)


@pytest.mark.asyncio
async def test_execution_client_rejects_plan_for_a_different_claim(monkeypatch):
    from simstack.runner_api import ExecutionRunnerClient, RunnerApiError

    client = ExecutionRunnerClient(
        server_url="https://example.test/api", runner_token="token"
    )

    async def request(*args, **kwargs):
        return {
            "task_id": "a" * 24,
            "claim_id": "other-claim",
            "attempt": 1,
            "program": "orca",
            "policy_revision": "c" * 64,
            "plan": {"command": ["orca"]},
        }

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(RunnerApiError, match="differs"):
        await client.get_program_plan(
            "a" * 24, claim_id="claim", attempt=1, program="orca"
        )


@pytest.mark.asyncio
async def test_supervisor_rejects_wrong_policy_acknowledgement(monkeypatch):
    from simstack.runner_api import RunnerApiError, SupervisorRunnerClient
    from simstack.runner_protocol.dto.programs import RunnerProgramPolicyRequestV1

    client = SupervisorRunnerClient(
        server_url="https://example.test/api", runner_token="token"
    )
    policy = RunnerProgramPolicyRequestV1(
        claim_id="claim",
        attempt=1,
        programs={"orca": NamedProgramPlanV1(command=["orca"])},
    )

    async def request(*args, **kwargs):
        return {
            "task_id": "a" * 24,
            "claim_id": "claim",
            "attempt": 1,
            "policy_revision": "0" * 64,
        }

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(RunnerApiError, match="differs"):
        await client.register_program_policy("a" * 24, policy=policy)


@pytest.mark.parametrize("command", ["VALUE=works env", "printf '%s' *.txt"])
def test_operator_shell_assignments_and_globs_keep_shell_semantics(tmp_path, command):
    (tmp_path / "found.txt").write_text("x")
    result = subprocess.run(
        invocation_argv(NamedProgramPlanV1(command=command), []),
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    assert (
        (b"VALUE=works" in result.stdout)
        if command.startswith("VALUE")
        else result.stdout == b"found.txt"
    )


@pytest.mark.asyncio
async def test_invocation_wrapper_cleans_only_its_own_subgroups(tmp_path):
    import os

    import simstack.runner_runtime.program_process as lifecycle
    from simstack.core.programs import _stop_process

    sibling = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", process_group=0
    )
    script = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],process_group=0); pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    invocation = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(lifecycle.__file__)),
        "--",
        sys.executable,
        "-c",
        script,
        cwd=tmp_path,
        process_group=0,
    )
    try:
        for _ in range(200):
            if (tmp_path / "child.pid").exists():
                break
            await asyncio.sleep(0.01)
        pid = int((tmp_path / "child.pid").read_text())
        await _stop_process(invocation)
        stat = Path(f"/proc/{pid}/stat")
        assert (
            not stat.exists() or stat.read_text().rpartition(") ")[2].split()[0] == "Z"
        )
        assert sibling.returncode is None
        os.kill(sibling.pid, 0)
    finally:
        await _stop_process(invocation)
        await _stop_process(sibling)


@pytest.mark.asyncio
async def test_invocation_wrapper_reaps_orphaned_native_subgroups(tmp_path):
    import simstack.runner_runtime.program_process as lifecycle

    script = "import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'],process_group=0); pathlib.Path('orphan.pid').write_text(str(p.pid))"
    invocation = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(lifecycle.__file__)),
        "--",
        sys.executable,
        "-c",
        script,
        cwd=tmp_path,
        process_group=0,
    )
    try:
        assert await asyncio.wait_for(invocation.wait(), timeout=5) == 0
        pid = int((tmp_path / "orphan.pid").read_text())
        stat = Path(f"/proc/{pid}/stat")
        assert (
            not stat.exists() or stat.read_text().rpartition(") ")[2].split()[0] == "Z"
        )
    finally:
        from simstack.core.programs import _stop_process

        await _stop_process(invocation)


@pytest.mark.asyncio
async def test_real_cached_filestack_is_staged_under_logical_input_name(
    monkeypatch, tmp_path
):
    import simstack.core.context as context_module
    import simstack.core.runner_execution.blobs as blobs_module
    from simstack.core.runner_execution.state import (
        RunnerClaimContext,
        activate_runner_claim,
        reset_runner_claim,
    )
    from simstack.models.parameters import Resource

    cache = tmp_path / "files" / "instance-cached"
    cache.mkdir(parents=True)
    original = cache / "probe-input.txt"
    original.write_text("cached-input-42")
    resource = Resource(value="r")
    value = FileStack.from_local_file(
        original,
        in_memory=False,
        secure_source=True,
        workdir=tmp_path,
        resource=resource,
    )

    class Api:
        async def get_program_plan(self, task_id, *, claim_id, attempt, program):
            return RunnerProgramPlanResponseV1(
                task_id=task_id,
                claim_id=claim_id,
                attempt=attempt,
                program=program,
                policy_revision="d" * 64,
                plan=NamedProgramPlanV1(
                    command=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('answer.txt').write_text(Path('probe-input.txt').read_text())",
                    ]
                ),
            )

    fake = SimpleNamespace(
        runner_api=Api(),
        runner_mode=True,
        initialized=True,
        worker_mode=True,
        config=SimpleNamespace(
            workdir=tmp_path, resource=resource, max_inline_document_bytes=1024
        ),
    )
    monkeypatch.setattr(context_module, "context", fake)
    monkeypatch.setattr(blobs_module, "publish_runtime_blobs", lambda values: None)
    token = activate_runner_claim(RunnerClaimContext("d" * 24, "claim", 1))
    try:
        # Demonstrate the real existing FileStack contract before invoking the SDK.
        requested = tmp_path / "tasks" / ("d" * 24) / "requested"
        requested.mkdir(parents=True)
        assert value.get(requested) == original
        result = await run_program(
            "cached", input_files=[value], output_names=["answer.txt"]
        )
    finally:
        reset_runner_claim(token)
    assert result.output_files[0].size == len("cached-input-42")
    staged = next(
        (tmp_path / "tasks" / ("d" * 24) / "programs").glob("*/probe-input.txt")
    )
    assert staged.read_text() == original.read_text() == "cached-input-42"
    assert staged != original


def test_input_staging_rejects_corruption_without_publishing(monkeypatch, tmp_path):
    from simstack.core.programs import _stage_program_input

    original = tmp_path / "original.txt"
    original.write_text("good")
    value = FileStack.from_local_file(original, in_memory=True)
    original.write_text("evil")
    destination = tmp_path / "invocation"
    destination.mkdir()
    with pytest.raises(ValueError, match="checksum"):
        _stage_program_input(value, original, destination)
    assert list(destination.iterdir()) == []


def test_input_staging_never_overwrites_an_existing_target(tmp_path):
    from simstack.core.programs import _stage_program_input

    original = tmp_path / "original.txt"
    original.write_text("good")
    value = FileStack.from_local_file(original, in_memory=True)
    destination = tmp_path / "invocation"
    destination.mkdir()
    (destination / value.name).write_text("keep")
    with pytest.raises(FileExistsError):
        _stage_program_input(value, original, destination)
    assert (destination / value.name).read_text() == "keep"


@pytest.mark.parametrize("arguments", ["one-argument", b"one-argument"])
def test_invocation_rejects_a_bare_argument_string(arguments):
    with pytest.raises(TypeError, match="sequence"):
        invocation_argv(NamedProgramPlanV1(command=["echo"]), arguments)
