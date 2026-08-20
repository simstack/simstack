import re
from typing import Optional, ClassVar, Dict, Any

from odmantic import Field, Model, EmbeddedModel
from pydantic import field_validator, model_validator


class SlurmParametersPatch(EmbeddedModel):
    nodes: Optional[int] = Field(default=None, ge=1)
    tasks: Optional[int] = Field(default=None, ge=1)
    tasks_per_node: Optional[int] = Field(default=None, ge=1)
    cpus_per_task: Optional[int] = Field(default=None, ge=1)
    mem: Optional[str] = None
    mem_per_cpu: Optional[str] = None
    time: Optional[str] = None
    begin: Optional[str] = None
    partition: Optional[str] = None
    qos: Optional[str] = None
    job_name: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    mail_type: Optional[str] = None
    mail_user: Optional[str] = None
    gres: Optional[str] = None
    account: Optional[str] = None
    priority: Optional[int] = None
    reservation: Optional[str] = None
    constraint: Optional[str] = None
    exclusive: Optional[bool] = None
    nice: Optional[int] = None
    dependency: Optional[str] = None
    array: Optional[str] = None
    startup_commands: Optional[list[str]] = None
    chdir: Optional[str] = None
    export: Optional[str] = None
    signal: Optional[str] = None
    requeue: Optional[bool] = None
    no_requeue: Optional[bool] = None

    model_config: ClassVar[Dict[str, Any]] = {
        "extra": "forbid",
    }


def _normalize_slurm_parameters_value(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (SlurmParametersPatch,)):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return SlurmParametersPatch.model_validate(value).model_dump(exclude_none=True)
    raise ValueError("slurm_parameters must be a dictionary or SlurmParametersPatch")


class ResourceAssignmentRule(Model):
    name: str = Field(unique=True)
    regex_pattern: str
    priority: int = Field(default=0)
    enabled: bool = Field(default=True)
    resource_str: Optional[str] = None
    queue: Optional[str] = None
    in_docker: bool = Field(default=False, description="Run in docker")
    force_rerun: bool = Field(default=False)
    recompute_artifacts: Optional[bool] = Field(
        default=False, description="Recompute artifacts for this node"
    )
    slurm_parameters: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = ""

    model_config = {"collection": "resource_assignment_rule"}

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_slurm_parameters_patch(cls, data: Any):
        if not isinstance(data, dict):
            return data

        payload = dict(data)
        legacy = payload.pop("slurm_parameters_patch", None)
        current = payload.get("slurm_parameters")
        if (current in (None, {}) or "slurm_parameters" not in payload) and legacy not in (
            None,
            {},
        ):
            payload["slurm_parameters"] = legacy
        return payload

    @field_validator("name", "regex_pattern", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        if value is None:
            return value
        return str(value).strip()

    @field_validator("resource_str", "queue", "description", mode="before")
    @classmethod
    def _strip_optional_strings(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("regex_pattern", mode="after")
    @classmethod
    def _validate_regex_pattern(cls, value: str) -> str:
        if not value:
            raise ValueError("Path pattern must not be empty")
        try:
            re.compile(cls.pattern_to_regex(value))
        except ValueError:
            raise
        except re.error as exc:
            raise ValueError(f"Invalid path pattern: {exc}") from exc
        return value

    @field_validator("slurm_parameters", mode="before")
    @classmethod
    def _normalize_slurm_parameters(cls, value):
        return _normalize_slurm_parameters_value(value)

    @model_validator(mode="after")
    def _validate_has_effect(self):
        has_slurm = bool(self.slurm_parameters)
        if not any(
            [
                self.resource_str,
                self.queue,
                has_slurm,
                self.in_docker,
                self.force_rerun,
                self.recompute_artifacts,
            ]
        ):
            raise ValueError(
                "ResourceAssignmentRule must set at least one of resource_str, queue, "
                "slurm_parameters, in_docker, force_rerun, or recompute_artifacts"
            )
        return self

    @property
    def slurm_parameters_patch(self) -> Dict[str, Any]:
        """Backward-compatible alias for older callers/tests."""
        return self.slurm_parameters

    @staticmethod
    def normalize_pattern(pattern: str) -> str:
        normalized = (pattern or "").strip()
        if normalized.startswith("."):
            without_leading_dots = normalized.lstrip(".")
            if without_leading_dots and "." not in without_leading_dots:
                return f"*.{without_leading_dots}"
            return without_leading_dots
        return normalized

    @classmethod
    def _pattern_segments(cls, pattern: str) -> list[str]:
        return [
            segment for segment in cls.normalize_pattern(pattern).split(".") if segment
        ]

    @staticmethod
    def _segment_to_regex(segment: str) -> str:
        return "".join("[^.]*" if char == "*" else re.escape(char) for char in segment)

    @classmethod
    def pattern_to_regex(cls, pattern: str) -> str:
        segments = cls._pattern_segments(pattern)
        if not segments:
            raise ValueError("Path pattern must not be empty")
        if segments == ["*"]:
            return r"[^.]+(?:\.[^.]+)*"

        regex_parts: list[str] = []
        for index, segment in enumerate(segments):
            if segment == "*":
                regex_parts.append(r"(?:[^.]+\.)*" if index == 0 else r"(?:\.[^.]+)*")
                continue

            segment_regex = cls._segment_to_regex(segment)
            previous_is_leading_wildcard = index == 1 and segments[0] == "*"
            if index == 0 or previous_is_leading_wildcard:
                regex_parts.append(segment_regex)
            else:
                regex_parts.append(r"\." + segment_regex)

        return "".join(regex_parts)

    @classmethod
    def pattern_specificity_score(cls, pattern: str) -> int:
        segments = cls._pattern_segments(pattern)
        literal_segment_count = sum(1 for segment in segments if segment != "*")
        literal_char_count = sum(len(segment.replace("*", "")) for segment in segments)
        wildcard_count = sum(segment.count("*") for segment in segments)
        return (
            literal_segment_count * 10000
            + literal_char_count * 100
            + len(segments)
            - wildcard_count
        )

    @classmethod
    def matches_call_path(cls, pattern: str, normalized_call_path: str) -> bool:
        return (
            re.fullmatch(cls.pattern_to_regex(pattern), normalized_call_path)
            is not None
        )
