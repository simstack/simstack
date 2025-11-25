from enum import Enum
from typing import Optional, List, ClassVar, Dict, Any

from odmantic import Field, EmbeddedModel
from pydantic import field_validator, model_validator

from simstack.core.resources import allowed_resources


class Resource(EmbeddedModel):
    """
    Resource whose value is validated against the allowed resources
    only when the value is *read*, not when it is *set* or constructed.
    """

    # Regular model field, no leading underscore
    value: str

    @classmethod
    def allowed_values(cls) -> List[str]:
        """
        Get the list of allowed resource values.
        """
        # Use the AllowedResources singleton
        return allowed_resources.get_resources()

    def _validate_value_on_read(self, v: str) -> str:
        """
        Validate the value against allowed resources when it is read.
        """
        if v not in self.allowed_values():
            allowed_str = ", ".join(repr(val) for val in self.allowed_values())
            raise ValueError(
                f"Invalid resource value: {v!r}. Allowed values are: {allowed_str}"
            )
        return v

    def __getattribute__(self, name: str):
        """
        Intercept reads of `value` and validate at access time.
        Setting/constructing does not perform validation.
        """
        if name == "value":
            # Get raw stored value without triggering this override again
            raw_value = object.__getattribute__(self, "__dict__").get("value")
            return object.__getattribute__(self, "_validate_value_on_read")(raw_value)
        return object.__getattribute__(self, name)

    def __str__(self) -> str:
        """String representation of the resource (validated on access)."""
        return self.value

    def __repr__(self) -> str:
        """Debug representation of the resource (validated on access)."""
        return f"{self.__class__.__name__}(value={self.value!r})"

    def __eq__(self, other) -> bool:
        """Equal comparison operator (uses validated value)."""
        if isinstance(other, Resource):
            return self.value == other.value
        elif isinstance(other, str):
            return self.value == other
        return False

    def __ne__(self, other) -> bool:
        """Not equal comparison operator."""
        return not self.__eq__(other)


class Queue(str, Enum):
    DEFAULT = "default"
    SLURM_QUEUE = "slurm-queue"


# TODO Fix Slurm Parameters
class SlurmParameters(EmbeddedModel):
    # Essential Resource Allocation Parameters
    nodes: Optional[int] = Field(default=1, ge=1, description="Number of compute nodes")
    tasks: Optional[int] = Field(
        default=1, ge=1, description="Total number of tasks/processes"
    )
    tasks_per_node: Optional[int] = Field(
        default=1, ge=1, description="Number of tasks per node"
    )
    cpus_per_task: Optional[int] = Field(
        default=1, ge=1, description="Number of CPU cores per task"
    )

    # Memory Parameters
    mem: Optional[str] = Field(
        default="1G", description="Memory per node (e.g., '32G', '1024M')"
    )
    mem_per_cpu: Optional[str] = Field(
        default=None, description="Memory per CPU core (e.g., '4G')"
    )

    # Time Parameters
    time: Optional[str] = Field(
        default="1:00:00",
        description="Maximum runtime (e.g., '24:00:00', '1-12:00:00')",
    )
    begin: Optional[str] = Field(
        default=None, description="Defer job start until specified time"
    )

    # Queue/Partition Parameters
    partition: Optional[str] = Field(
        default=None, description="Queue/partition to submit to"
    )
    qos: Optional[str] = Field(default=None, description="Quality of Service level")

    # Job Information Parameters
    job_name: Optional[str] = Field(default="simstack", description="Job name")
    output: Optional[str] = Field(default=None, description="Standard output file path")
    error: Optional[str] = Field(default=None, description="Standard error file path")

    # Notification Parameters
    mail_type: Optional[str] = Field(
        default=None, description="Email notification triggers (BEGIN,END,FAIL,ALL)"
    )
    mail_user: Optional[str] = Field(
        default=None, description="Email address for notifications"
    )

    # GPU Parameters
    gres: Optional[str] = Field(
        default=None, description="Generic resources (e.g., 'gpu:2', 'gpu:v100:1')"
    )

    # Advanced Scheduling Parameters
    account: Optional[str] = Field(default=None, description="Billing account")
    priority: Optional[int] = Field(default=None, description="Job priority")
    reservation: Optional[str] = Field(
        default=None, description="Use specific reservation"
    )
    constraint: Optional[str] = Field(
        default=None, description="Node feature constraints"
    )
    exclusive: Optional[bool] = Field(default=None, description="Exclusive node access")
    nice: Optional[int] = Field(default=None, description="Adjust scheduling priority")

    # Dependency and Array Parameters
    dependency: Optional[str] = Field(
        default=None, description="Job dependencies (e.g., 'afterok:jobid')"
    )
    array: Optional[str] = Field(
        default=None, description="Job arrays (e.g., '1-100', '1-10:2')"
    )

    # Additional Commands
    startup_commands: List[str] = Field(
        default_factory=list, description="Commands to run before main job"
    )

    # Working Directory
    chdir: Optional[str] = Field(
        default=None, description="Working directory for the job"
    )

    # Export Environment
    export: Optional[str] = Field(
        default=None, description="Environment variables to export"
    )

    # Signal handling
    signal: Optional[str] = Field(
        default=None, description="Signal to send when time limit is reached"
    )

    # Requeue options
    requeue: Optional[bool] = Field(
        default=None, description="Allow job to be requeued"
    )
    no_requeue: Optional[bool] = Field(
        default=None, description="Prevent job from being requeued"
    )

    model_config: ClassVar[Dict[str, Any]] = {
        "extra": "forbid",
        "json_schema_extra": {
            "title": "SlurmParameters",
            "description": "Comprehensive parameters for Slurm job submission",
            "examples": [
                {
                    "nodes": 2,
                    "ntasks_per_node": 8,
                    "cpus_per_task": 4,
                    "mem": "64G",
                    "time": "12:00:00",
                    "partition": "compute",
                    "gres": "gpu:2",
                    "job_name": "my_simulation",
                    "output": "job_%j.out",
                    "error": "job_%j.err",
                    "mail_type": "END,FAIL",
                    "mail_user": "user@institution.edu",
                }
            ],
        },
    }

    def to_sbatch_args(self) -> List[str]:
        """Convert parameters to SBATCH arguments list."""
        args = []

        # Validate memory parameters first
        memory_options = []
        if self.mem is not None:
            memory_options.append("mem")
        if self.mem_per_cpu is not None:
            memory_options.append("mem_per_cpu")
        if hasattr(self, "mem_per_gpu") and getattr(self, "mem_per_gpu") is not None:
            memory_options.append("mem_per_gpu")

        if len(memory_options) > 1:
            raise ValueError(
                f"SLURM memory parameters are mutually exclusive. "
                f"Found: {', '.join(memory_options)}. "
                f"Please specify only one of: --mem, --mem-per-cpu, or --mem-per-gpu"
            )

        # Map field names to SBATCH parameters
        field_mapping = {
            "nodes": "--nodes",
            "tasks": "--ntasks",
            "tasks_per_node": "--ntasks-per-node",
            "cpus_per_task": "--cpus-per-task",
            "mem": "--mem",
            "mem_per_cpu": "--mem-per-cpu",
            "time": "--time",
            "begin": "--begin",
            "partition": "--partition",
            "qos": "--qos",
            "job_name": "--job-name",
            "output": "--output",
            "error": "--error",
            "mail_type": "--mail-type",
            "mail_user": "--mail-user",
            "gres": "--gres",
            "account": "--account",
            "priority": "--priority",
            "reservation": "--reservation",
            "constraint": "--constraint",
            "dependency": "--dependency",
            "array": "--array",
            "chdir": "--chdir",
            "export": "--export",
            "signal": "--signal",
        }

        # Add mem_per_gpu to field mapping if it exists
        if hasattr(self, "mem_per_gpu"):
            field_mapping["mem_per_gpu"] = "--mem-per-gpu"

        # Add parameters with values
        for field_name, sbatch_param in field_mapping.items():
            value = getattr(self, field_name, None)
            if value is not None:
                args.append(f"{sbatch_param}={value}")

        # Add boolean flags
        if self.exclusive:
            args.append("--exclusive")
        if self.requeue:
            args.append("--requeue")
        if self.no_requeue:
            args.append("--no-requeue")
        if self.nice is not None:
            args.append(f"--nice={self.nice}")

        return args

    def to_sbatch_header(self) -> str:
        """Convert parameters to SBATCH header string for script files."""
        lines = ["#!/bin/bash"]

        for arg in self.to_sbatch_args():
            lines.append(f"#SBATCH {arg}")

        if self.startup_commands:
            lines.append("")
            lines.extend(self.startup_commands)

        return "\n".join(lines)


class Parameters(EmbeddedModel):
    force_rerun: bool = False
    resource: Resource = Field(default_factory=lambda: Resource(value="self"))
    queue: str = Field(default="default")
    recompute_artifacts: Optional[bool] = Field(
        default=False, description="Recompute artifacts for this node"
    )

    other_value: str = Field(default="other")
    test_dict: Dict[str, Any] = Field(default_factory=lambda: {"test": "value"})

    slurm_parameters: SlurmParameters = Field(default=None)
    # slurm_parameters_data: Dict[str, Any] = Field(default_factory=dict)

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "title": "Parameters",
            "description": "Parameters for running a simulation",
            "examples": [
                {
                    "resource": "self",
                    "queue": "default",
                    "slurm_parameters": {
                        "nodes": 2,
                    },
                }
            ],
        },
    }

    @model_validator(mode="before")
    @classmethod
    def migrate_slurm_parameters(cls, data):
        if "slurm_parameters_data" in data and "slurm_parameters" not in data:
            data["slurm_parameters"] = SlurmParameters(**data["slurm_parameters_data"])
            del data["slurm_parameters_data"]
        if "slurm_parameters" not in data or data["slurm_parameters"] is None:
            data["slurm_parameters"] = SlurmParameters()
        return data

    @field_validator("resource", mode="before")
    @classmethod
    def validate_resource(cls, v):
        """
        Validate and convert resource input to a Resource object.

        Accepts string, Resource objects, and dictionary representations.
        If a string is provided, converts it to a Resource object.
        If a dictionary is provided (e.g., during deserialization),
        extracts the value and creates a Resource object.

        Args:
            v: The value to validate (str, Resource, or dict)

        Returns:
            Resource: The validated Resource object
        """
        if isinstance(v, str):
            return Resource(value=v)
        elif isinstance(v, Resource):
            return v
        elif isinstance(v, dict):
            # Handle deserialization from the dictionary
            if "value" in v:
                return Resource(value=v["value"])
            else:
                raise ValueError(
                    f"Dictionary representation of resource must contain 'value' key, got {v}"
                )
        else:
            raise ValueError(
                f"resource must be a string, Resource object, or dictionary, got {type(v)}"
            )

    # # property getters and setters to handle the slurm_parameters
    # @property
    # def slurm_parameters(self) -> Optional[SlurmParameters]:
    #     if not self.slurm_parameters_data:
    #         default_slurm = SlurmParameters()
    #         self.slurm_parameters_data = default_slurm.model_dump()
    #     return SlurmParameters(**self.slurm_parameters_data)
    #
    # @slurm_parameters.setter
    # def slurm_parameters(self, value: Optional[SlurmParameters]) -> None:
    #     if value is None:
    #         self.slurm_parameters_data = {}
    #     else:
    #         self.slurm_parameters_data = value.model_dump()
    #
    # # Add any convenience methods to work with slurm parameters
    # def set_slurm_config(self, **kwargs):
    #     """Update slurm parameters with the given keyword arguments."""
    #     current = self.slurm_parameters
    #     if current is None:
    #         current = SlurmParameters()
    #
    #     for key, value in kwargs.items():
    #         if hasattr(current, key):
    #             setattr(current, key, value)
    #
    #     self.slurm_parameters = current
