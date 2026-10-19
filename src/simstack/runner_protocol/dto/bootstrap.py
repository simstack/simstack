"""Canonical runner bootstrap DTOs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..constants import RUNNER_API_VERSION, RunnerQueue, RunnerScope
from .base import CanonicalString, CanonicalTimestamp, StrictRunnerDto


class RunnerResourceV1(StrictRunnerDto):
    resource_str: CanonicalString
    workdir: CanonicalString
    hostname: CanonicalString
    python_paths: list[CanonicalString]
    environment_start: CanonicalString | None
    is_default: bool


class RunnerCapabilitiesV1(StrictRunnerDto):
    max_runner_payload_bytes: int | None = Field(gt=0)
    max_inline_document_bytes: int = Field(gt=0)
    workflow_installations: bool = False
    object_storage: bool = False
    dataset_storage: bool = False

class RunnerDataMappingV1(StrictRunnerDto):
    name: CanonicalString
    mapping: CanonicalString
    description: str | None
    version: str | None


class RunnerModelMappingV1(StrictRunnerDto):
    id: CanonicalString
    name: CanonicalString
    mapping: CanonicalString
    version: str | None
    json_schema: str | None
    ui_schema: str | None
    workflow_package: CanonicalString | None = None
    workflow_installation_id: CanonicalString | None = None
    workflow_revision: CanonicalString | None = None
    runtime_profile: CanonicalString | None = None


class RunnerResourceSelectionV1(StrictRunnerDto):
    value: CanonicalString


class RunnerSlurmParametersV1(StrictRunnerDto):
    nodes: int | None
    tasks: int | None
    tasks_per_node: int | None
    cpus_per_task: int | None
    mem: str | None
    mem_per_cpu: str | None
    time: str | None
    begin: str | None
    partition: str | None
    qos: str | None
    job_name: str | None
    output: str | None
    error: str | None
    mail_type: str | None
    mail_user: str | None
    gres: str | None
    account: str | None
    priority: int | None
    reservation: str | None
    constraint: str | None
    exclusive: bool | None
    nice: int | None
    dependency: str | None
    array: str | None
    startup_commands: list[str]
    chdir: str | None
    export: str | None
    signal: str | None
    requeue: bool | None
    no_requeue: bool | None


class RunnerDefaultParametersV1(StrictRunnerDto):
    force_rerun: bool
    resource: RunnerResourceSelectionV1
    queue: RunnerQueue
    in_docker: bool
    recompute_artifacts: bool | None
    slurm_parameters: RunnerSlurmParametersV1 | None


class RunnerNodeModelV1(StrictRunnerDto):
    id: CanonicalString
    name: CanonicalString
    function_mapping: CanonicalString
    version: str | None
    input_mappings: list[RunnerDataMappingV1]
    result_mappings: list[RunnerDataMappingV1]
    called_nodes: list[CanonicalString]
    description: str | None
    favorite: bool
    default_parameters: RunnerDefaultParametersV1
    workflow_package: CanonicalString | None = None
    workflow_installation_id: CanonicalString | None = None
    workflow_revision: CanonicalString | None = None
    runtime_profile: CanonicalString | None = None


class RunnerWorkflowPackageV1(StrictRunnerDto):
    installation_id: CanonicalString
    package_id: CanonicalString
    revision: CanonicalString = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: CanonicalString
    manifest: dict


class RunnerBootstrapV1(StrictRunnerDto):
    api_version: Literal[RUNNER_API_VERSION]
    server_time: CanonicalTimestamp
    resource: RunnerResourceV1
    allowed_resources: list[CanonicalString]
    model_mappings: list[RunnerModelMappingV1]
    node_models: list[RunnerNodeModelV1]
    scopes: list[RunnerScope]
    capabilities: RunnerCapabilitiesV1
    workflow_packages: list[RunnerWorkflowPackageV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_lists(self) -> "RunnerBootstrapV1":
        if len(self.allowed_resources) != len(set(self.allowed_resources)):
            raise ValueError("allowed_resources must be unique")
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("scopes must be unique")
        identities = [(item.package_id, item.revision) for item in self.workflow_packages]
        if len(identities) != len(set(identities)):
            raise ValueError("workflow_packages must have unique identities")
        return self
