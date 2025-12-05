from typing import Optional, List
import urllib.parse
import os
from pathlib import Path
from odmantic import Model, Field, EmbeddedModel
from pydantic import field_validator

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
    resource: str = Field(unique=True)
    ssh_key_path: Optional[str]   
    workdir: str
    python_path: List[str]
    environment_start: str
    routes: Optional[List[str]] = [] # this is the list of resources that this resource can reach by ssh

    @field_validator("ssh_key_path", mode="after")
    @classmethod
    def validate_ssh_key(cls, v):
        if v is not None:
            path = Path(v)
            if not path.is_file():
                raise ValueError(f"SSH key path does not exist: {v}")
        return v

    @field_validator("python_path", mode="after")
    @classmethod
    def validate_python_path(cls, v):
        for path in v:
            if not os.path.exists(path):
                raise ValueError(f"Python path does not exist: {path}")
        return v

    def __str__(self):
        return f"ResourceDefinition(name={self.name},\n ssh_key_path={self.ssh_key_path}, workdir={self.workdir}, \npython_path={self.python_path}, \nenvironment_start={self.environment_start})"

    def __repr__(self):
        return f"ResourceDefinition(name={self.name})"

    @classmethod
    def from_resource_definition(cls, resource_definition: dict):
        return cls(**resource_definition)