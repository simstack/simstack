"""Minimal pattern for replaying a captured complex-node execution."""

from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from simstack.models import Parameters


def replay_generated_execution(
    node: Callable[..., Any],
    side_effect: Callable[..., Any],
    input_model: Any,
    *,
    test_root: Path,
    node_name: str,
) -> Any:
    """Run ``node`` while replacing its expensive side effect with saved data."""

    def patched_side_effect(*args: Any, **kwargs: Any) -> str:
        test_dir = test_root / node_name / kwargs["arg_hash"]
        return (test_dir / "task_id.txt").read_text().strip()

    patch_target = f"{side_effect.__module__}.{side_effect.__name__}"
    with patch(patch_target, side_effect=patched_side_effect):
        return node(
            input_model,
            parameters=Parameters(force_rerun=True),
        )
