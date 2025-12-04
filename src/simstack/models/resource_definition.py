from typing import Optional, List
import urllib.parse
import os
from pathlib import Path
from odmantic import Model, Field, EmbeddedModel
from pydantic import field_validator


class GitRepo(EmbeddedModel):
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
    name: str = Field(unique=True)
    ssh_key_path: Optional[str]   
    workdir: str
    python_path: List[str]
    environment_start: str
    git: List[GitRepo]

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
