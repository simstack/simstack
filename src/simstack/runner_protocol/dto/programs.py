"""Claim-bound, operator-owned named-program invocation plans."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import Field, ValidationInfo, field_validator

from .base import CanonicalString, StrictRunnerDto


class NamedProgramPlanV1(StrictRunnerDto):
    command: str | list[str]
    scripts: list[str] = Field(default_factory=list, max_length=100)
    shell: list[str] | None = Field(default=None, max_length=10)
    environment_allowlist: list[str] = Field(default_factory=list, max_length=100)
    timeout_seconds: int = Field(default=3600, ge=1, le=604800)

    @field_validator("command", "scripts", "shell")
    @classmethod
    def command_values(cls, value, info: ValidationInfo):
        if value is None:
            return value
        values = [value] if isinstance(value, str) else value
        if (not values and info.field_name != "scripts") or any(
            not item.strip() or "\x00" in item or len(item) > 100_000 for item in values
        ):
            raise ValueError("Program commands must contain bounded nonempty strings")
        return value


class RunnerProgramPolicyRequestV1(StrictRunnerDto):
    claim_id: CanonicalString = Field(max_length=200)
    attempt: int = Field(ge=1)
    programs: dict[str, NamedProgramPlanV1] = Field(max_length=256)

    @field_validator("programs")
    @classmethod
    def named_programs(cls, value):
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
            for name in value
        ):
            raise ValueError("Program names must be bounded identifiers")
        raw = json.dumps(
            {key: item.model_dump(mode="json") for key, item in value.items()},
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(raw.encode()) > 256 * 1024:
            raise ValueError("Named-program policy exceeds 256 KiB")
        return value

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


class RunnerProgramPlanResponseV1(StrictRunnerDto):
    task_id: CanonicalString
    claim_id: CanonicalString
    attempt: int = Field(ge=1)
    program: CanonicalString
    policy_revision: CanonicalString = Field(pattern=r"^[0-9a-f]{64}$")
    plan: NamedProgramPlanV1


class RunnerProgramPolicyResponseV1(StrictRunnerDto):
    task_id: CanonicalString
    claim_id: CanonicalString
    attempt: int = Field(ge=1)
    policy_revision: CanonicalString = Field(pattern=r"^[0-9a-f]{64}$")
