import asyncio
import os

import pytest

from simstack.core.context import context
from simstack.core.definitions import TaskStatus
from simstack.core.node import node
from simstack.models import (
    FloatData,
    NamedDataReference,
    NodeRegistry,
    Parameters,
    Project,
)


@node
def remote_runner_process_identity(_input: FloatData, **_kwargs) -> FloatData:
    """Return the PID so the test can prove execution happened in the runner."""
    return FloatData(field_name="runner_pid", value=float(os.getpid()))


@pytest.mark.asyncio
@pytest.mark.local_runner
async def test_subprocess_runner_completes_submitted_node(test_runner):
    if test_runner is None:
        pytest.skip(
            "Set START_LOCAL_RUNNER=true with a real disposable MongoDB to run this gate."
        )

    project = await context.db.save(Project(field_name="runner-completion-project"))
    input_value = await context.db.save(FloatData(field_name="input", value=1.0))
    function_mapping = (
        f"{remote_runner_process_identity.__module__}."
        f"{remote_runner_process_identity.__name__}"
    )
    submitted = await context.db.save(
        NodeRegistry(
            name=remote_runner_process_identity.__name__,
            status=TaskStatus.SUBMITTED,
            project=project.id,
            function_hash="NOT INITIALIZED",
            arg_hash="NOT INITIALIZED",
            func_mapping=function_mapping,
            call_path=f".{remote_runner_process_identity.__name__}",
            input_references=[
                NamedDataReference(
                    variable_name="_input",
                    variable_mapping="simstack.models.base_types.FloatData",
                    reference=input_value.id,
                )
            ],
            parameters=Parameters(resource="test", force_rerun=True),
        )
    )

    deadline = asyncio.get_running_loop().time() + 30
    completed = None
    while asyncio.get_running_loop().time() < deadline:
        return_code = test_runner.poll()
        if return_code is not None:
            runner_output = "\n".join(getattr(test_runner, "simstack_output_lines", ()))
            pytest.fail(
                f"simstack_runner exited with code {return_code} while the node was pending.\n"
                f"{runner_output}"
            )

        current = await context.db.find_one(
            NodeRegistry, NodeRegistry.id == submitted.id
        )
        assert current is not None
        if current.status == TaskStatus.COMPLETED:
            completed = current
            break
        if current.status == TaskStatus.FAILED:
            pytest.fail(
                f"Runner marked node {current.id} failed: "
                f"error={current.error!r}, message={current.message!r}"
            )
        await asyncio.sleep(0.2)

    assert completed is not None, (
        "Runner did not complete the submitted node within 30 seconds."
    )
    assert completed.started_at is not None
    assert completed.completed_at is not None
    assert len(completed.results_references) == 1

    result_reference = completed.results_references[0]
    assert result_reference.variable_mapping == "simstack.models.base_types.FloatData"
    result = await context.db.find_one(
        FloatData, FloatData.id == result_reference.reference
    )
    assert result is not None
    assert result.field_name == "runner_pid"
    assert int(result.value) == test_runner.pid
    assert int(result.value) != os.getpid()
