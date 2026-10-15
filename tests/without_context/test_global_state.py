from __future__ import annotations

from pathlib import Path

import pytest

from simstack.core.context import GlobalState
from tests.support.in_memory_repository import InMemoryRepository


@pytest.fixture
def state(monkeypatch) -> GlobalState:
    monkeypatch.setattr(GlobalState, "_instance", None)
    return GlobalState()


def test_global_state_is_a_singleton(state: GlobalState) -> None:
    assert GlobalState() is state


@pytest.mark.asyncio
async def test_initialize_requires_an_explicit_repository(
    state: GlobalState,
) -> None:
    with pytest.raises(TypeError, match="repository"):
        await state.initialize()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_non_runner_context_exposes_only_the_repository_port(
    state: GlobalState,
    tmp_path: Path,
) -> None:
    repository = InMemoryRepository()
    await state.initialize(
        repository=repository,
        project_root=tmp_path,
        skip_config=True,
        is_test=True,
    )

    assert state.repository is repository
    assert not hasattr(state, "db")


@pytest.mark.asyncio
async def test_runner_boundary_cannot_receive_a_repository(
    state: GlobalState,
    tmp_path: Path,
) -> None:
    state._runner_boundary = True

    with pytest.raises(
        RuntimeError,
        match="runner context cannot be reinitialized with a repository",
    ):
        await state.initialize(
            repository=InMemoryRepository(),
            project_root=tmp_path,
            skip_config=True,
            is_test=True,
        )
