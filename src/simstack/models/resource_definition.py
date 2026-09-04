from typing import Optional, List
import urllib.parse
import socket
import re
from pathlib import Path
from odmantic import EmbeddedModel, Field, Model
from pydantic import field_validator, model_serializer

from simstack.models.parameters import normalize_execution_queue
from simstack.util.transform_file_name import transform_file_name
class GitRepo(Model):
    """
    Represents a Git repository with relevant attributes such as its URL, branch,
    and whether it is a submodule. Ensures that the URL provided is valid.

    This class is used to model information about a Git repository, including
    its URL, the branch being used, and whether it is included as a submodule
    within another repository. It provides validation for the URL to ensure that
    it is in the correct format.

    In the user database there is a list of Git repositories for the user

    :ivar url: The URL of the Git repository.
    :type url: str
    :ivar branch: The branch of the Git repository. Optional.
    :type branch: Optional[str]
    :ivar is_submodule: Indicates whether the repository is a submodule. Defaults to False.
    :type is_submodule: bool
    """
    url: str
    branch: Optional[str]  
    is_submodule: bool = Field(default=False)

    @field_validator("url", mode="after")
    @classmethod
    def validate_url(cls, v):
        try:
            result = urllib.parse.urlparse(v)
            if not all([result.scheme, result.netloc]):
                raise ValueError("Invalid URL format")
        except Exception:
            raise ValueError("Invalid URL format")
        return v

class ResourceDefinition(Model):
    resource_str: str = Field(unique=True)
    workdir: str  # Change Path to str
    hostname: str
    python_paths: List[str] = Field(default_factory=list)  # Change List[Path] to List[str]
    environment_start: Optional[str] = None
    ssh_key: Optional[str] = None  # Change Optional[Path] to Optional[str]
    routes: Optional[List[str]] = []
    queue: str = "default"
    is_default: bool = False
    git_branch: str = "main"

    @staticmethod
    def _convert_backslashes(path_str: str) -> str:
        return re.sub(r'\\+', '/', path_str)

    @field_validator("workdir", mode="before")
    @classmethod
    def convert_workdir(cls, v):
        if isinstance(v, (Path, str)):
            return str(cls._convert_backslashes(str(v)))
        return v

    @field_validator("python_paths", mode="before")
    @classmethod
    def convert_python_paths(cls, v):
        if isinstance(v, list):
            return [str(cls._convert_backslashes(str(p))) for p in v]
        return v

    @field_validator("ssh_key", mode="before")
    @classmethod
    def convert_ssh_key(cls, v):
        if v is None:
            return None
        return str(cls._convert_backslashes(str(v)))

    @field_validator("queue", mode="before")
    @classmethod
    def normalize_queue(cls, v):
        queue, _ = normalize_execution_queue(v)
        return queue


    def validate_hostname(self):
        current_hostname = socket.gethostname()
        if self.hostname != current_hostname:
            raise ValueError(f"Hostname must match current host. Expected: {current_hostname}, got: {self.hostname}")

    def validate_ssh_key(self):
        if self.ssh_key is not None:
            file_path = transform_file_name(Path(self.ssh_key)) # Convert to Path for utility
            if not file_path:
                raise ValueError(f"SSH key path does not exist: {self.ssh_key}")

    def get_ssh_key_path(self):
        if self.ssh_key is not None:
            return transform_file_name(Path(self.ssh_key))
        return None

    def validate_python_path(self):
        for path_str in self.python_paths:
            path = Path(path_str)
            real_path = transform_file_name(path)
            if not path.exists():
                raise ValueError(f"Python path does not exist: {path}")
            if not path.is_dir():
                raise ValueError(f"Python path is not a directory: {path}")

    def get_python_path(self):
        if self.python_paths:
            return [transform_file_name(Path(p)) for p in self.python_paths]
        return None

    def __repr__(self):
        return f"ResourceDefinition(name={self.resource_str})"

    @classmethod
    def from_resource_definition(cls, resource_definition: dict):
        return cls(**resource_definition)
