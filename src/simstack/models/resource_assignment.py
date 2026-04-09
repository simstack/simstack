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


class ResourceAssignmentRule(Model):
    name: str = Field(unique=True)
    regex_pattern: str
    priority: int = Field(default=0)
    enabled: bool = Field(default=True)
    resource_str: Optional[str] = None
    queue: Optional[str] = None
    slurm_parameters_patch: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = ""

    model_config = {"collection": "resource_assignment_rule"}

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
            raise ValueError("regex_pattern must not be empty")
        try:
            re.compile(cls.normalize_pattern(value))
        except re.error as exc:
            raise ValueError(f"Invalid regex_pattern: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _validate_has_effect(self):
        has_slurm_patch = bool(self.slurm_parameters_patch)
        if not any([self.resource_str, self.queue, has_slurm_patch]):
            raise ValueError(
                "ResourceAssignmentRule must set at least one of resource_str, queue, or slurm_parameters_patch"
            )
        return self

    @staticmethod
    def normalize_pattern(pattern: str) -> str:
        normalized = (pattern or "").strip()
        if normalized.startswith("."):
            return normalized[1:]
        return normalized
    @field_validator("slurm_parameters_patch", mode="before")
    @classmethod
    def _normalize_slurm_patch(cls, value):
        if value is None:
            return {}
        if isinstance(value, SlurmParametersPatch):
            return value.model_dump(exclude_none=True)
        if isinstance(value, dict):
            return SlurmParametersPatch.model_validate(value).model_dump(exclude_none=True)
        raise ValueError("slurm_parameters_patch must be a dictionary or SlurmParametersPatch")
