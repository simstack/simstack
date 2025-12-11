from typing import Optional, List
import urllib.parse
import socket
import re
from pathlib import Path
from odmantic import Model, Field, EmbeddedModel
from pydantic import field_validator

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
    workdir: Path
    hostname: str
    python_paths: List[Path] = Field(default_factory=list)
    environment_start: Optional[str] = None
    ssh_key: Optional[Path] = None
    routes: Optional[List[str]] = [] # this is the list of resources that this resource can reach by ssh

    @staticmethod
    def _convert_backslashes(path_str: str) -> str:
        return re.sub(r'\\+', '/', path_str)

    @field_validator("workdir", mode="before")
    @classmethod
    def convert_workdir(cls, v):
        if isinstance(v, str):
            return Path(cls._convert_backslashes(v))
        return v

    @field_validator("python_paths", mode="before")
    @classmethod
    def convert_python_paths(cls, v):
        if isinstance(v, list):
            return [Path(cls._convert_backslashes(p)) if isinstance(p, str) else p for p in v]
        return v

    @field_validator("ssh_key", mode="before")
    @classmethod
    def convert_ssh_key(cls, v):
        if isinstance(v, str):
            return Path(cls._convert_backslashes(v))
        return v

    def validate_hostname(self):
        current_hostname = socket.gethostname()
        if self.hostname != current_hostname:
            raise ValueError(f"Hostname must match current host. Expected: {current_hostname}, got: {v}")

    def validate_ssh_key(self):
        if self.ssh_key is not None:
            file_path = transform_file_name(self.ssh_key)
            if not file_path:
                raise ValueError(f"SSH key path does not exist: {v}")

    def get_ssh_key_path(self):
        if self.ssh_key is not None:
            return transform_file_name(self.ssh_key)
        return None

    def validate_python_path(self):
        for path in self.python_paths:
            real_path = transform_file_name(path)
            if not path.exists():
                raise ValueError(f"Python path does not exist: {path}")
            if not path.is_dir():
                raise ValueError(f"Python path is not a directory: {path}")

    def get_python_path(self):
        if self.python_path is not None:
            return [ transform_file_name(p) for p in self.python_path]
        return None

    def __str__(self):
        return f"ResourceDefinition(name={self.resource_str},\n ssh_key_path={self.ssh_key}, workdir={self.workdir}, \npython_path={self.python_paths}, \nenvironment_start={self.environment_start})"

    def __repr__(self):
        return f"ResourceDefinition(name={self.resource_str})"

    @classmethod
    def from_resource_definition(cls, resource_definition: dict):
        return cls(**resource_definition)