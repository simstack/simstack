import json
from dataclasses import asdict, dataclass


RESULT_PREFIX = "SIMSTACK_RUN_NODE_RESULT="


@dataclass(frozen=True)
class RunNodeResult:
    success: bool
    return_kind: str
    error: str | None = None


def encode_run_node_result(result: RunNodeResult) -> str:
    payload = json.dumps(asdict(result), separators=(",", ":"))
    return f"{RESULT_PREFIX}{payload}"


def parse_run_node_result(output: str) -> RunNodeResult | None:
    for line in reversed(output.splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            payload = json.loads(line.removeprefix(RESULT_PREFIX))
            if not isinstance(payload.get("success"), bool):
                return None
            if payload.get("return_kind") not in {
                "bool",
                "exception",
                "model",
                "multiple",
                "none",
            }:
                return None
            error = payload.get("error")
            if error is not None and not isinstance(error, str):
                return None
            return RunNodeResult(
                success=payload["success"],
                return_kind=payload["return_kind"],
                error=error,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None
