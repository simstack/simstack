"""Prepare uv-locked project snapshots without resolving a second dependency graph.

Network-enabled preparation installs wheels only. Source builds happen in the
caller's operator-owned boundary through the separately returned offline command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

MAX_PROJECT_METADATA_BYTES = 16 * 1024 * 1024
_DATABASE_DISTRIBUTIONS = frozenset({"odmantic", "motor", "pymongo", "bson"})


@dataclass(frozen=True)
class UvProject:
    root: Path
    name: str
    source_packages: tuple[str, ...]
    build_requirements: tuple[str, ...]
    identity: dict[str, str]
    core_packages: tuple[dict[str, Any], ...]
    dependencies: tuple[str, ...]
    build_requirement_groups: tuple[tuple[str, ...], ...] = ()


def _metadata(path: Path) -> tuple[bytes, dict]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"uv project metadata must be a regular file: {path.name}")
    data = path.read_bytes()
    if len(data) > MAX_PROJECT_METADATA_BYTES:
        raise ValueError("uv project metadata exceeds its size budget")
    return data, tomllib.loads(data.decode("utf-8"))


def _contained(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or "\\" in relative
    ):
        raise ValueError("uv source must be a contained relative project path")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError("uv source escapes its immutable project snapshot")
    return path


def _public_artifact(url: str) -> None:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname not in {"pypi.org", "files.pythonhosted.org"}
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.fragment
    ):
        raise ValueError(
            "uv runtime artifacts must use the approved public PyPI endpoints"
        )


def _selected(package: dict) -> bool:
    markers = package.get("resolution-markers", [])
    return not markers or any(Marker(value).evaluate() for value in markers)


def load_uv_project(source: str | Path) -> UvProject | None:
    """Read the authoritative uv graph; legacy manifest-only archives return None.

    Every non-Core VCS/path package must have been materialized into the source
    snapshot. Runtime preparation never fetches a mutable Git branch or a local
    path from the machine that originally produced the archive.
    """
    root = Path(source).resolve(strict=True)
    if not (root / "uv.lock").exists():
        return None
    project_bytes, project = _metadata(root / "pyproject.toml")
    lock_bytes, lock = _metadata(root / "uv.lock")
    if lock.get("version") != 1 or not isinstance(lock.get("package"), list):
        raise ValueError("Unsupported or incomplete uv.lock")
    raw_name = project.get("project", {}).get("name")
    if not isinstance(raw_name, str) or not raw_name:
        raise ValueError("uv workflow projects require a project.name")
    name = canonicalize_name(raw_name)
    requirements = project.get("project", {}).get("dependencies", [])
    if not isinstance(requirements, list):
        raise TypeError("uv project dependencies must be a requirement list")
    for value in requirements:
        requirement = Requirement(value)
        if (
            requirement.marker is None or requirement.marker.evaluate()
        ) and canonicalize_name(requirement.name) in _DATABASE_DISTRIBUTIONS:
            raise ValueError(
                "uv workflow projects must migrate database dependencies to the API document contract"
            )
    roots: dict[str, Path] = {name: root}
    core = []
    for package in lock["package"]:
        package_name = canonicalize_name(package["name"])
        location = package.get("source", {})
        if package_name == "simstack":
            if _selected(package):
                core.append(package)
            continue
        if "registry" in location:
            _public_artifact(location["registry"])
            for artifact in [package.get("sdist"), *package.get("wheels", [])]:
                if artifact is not None:
                    _public_artifact(artifact["url"])
            continue
        relative = location.get(
            "directory", location.get("editable", location.get("virtual"))
        )
        if relative is None:
            raise ValueError(
                f"uv source {package_name} is not bundled; create a complete workflow snapshot first"
            )
        path = _contained(root, relative)
        prior = roots.get(package_name)
        if prior is not None and prior != path:
            raise ValueError("uv snapshot contains ambiguous source package identities")
        roots[package_name] = path
    snapshot = root / ".simstack" / "snapshot.json"
    identity = {
        "pyproject_sha256": hashlib.sha256(project_bytes).hexdigest(),
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
    }
    if snapshot.exists():
        from simstack.workflows.snapshot import read_snapshot

        read_snapshot(root)
        identity["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    build_requirements = []
    build_groups = []
    for package_name, path in sorted(roots.items()):
        _, declaration = _metadata(path / "pyproject.toml")
        declared_name = declaration.get("project", {}).get("name")
        if declared_name and canonicalize_name(declared_name) != package_name:
            raise ValueError(
                "uv source metadata disagrees with its locked package identity"
            )
        group = []
        for value in declaration.get("build-system", {}).get("requires", []):
            requirement = Requirement(value)
            if requirement.url is not None or canonicalize_name(
                requirement.name
            ) in _DATABASE_DISTRIBUTIONS | {"simstack"}:
                raise ValueError(
                    "Workflow build backends must use ordinary package-index dependencies"
                )
            build_requirements.append(str(requirement))
            group.append(str(requirement))
        if group:
            build_groups.append(tuple(group))
    return UvProject(
        root,
        name,
        tuple(sorted(roots)),
        tuple(dict.fromkeys(build_requirements)),
        identity,
        tuple(core),
        tuple(requirements),
        tuple(dict.fromkeys(build_groups)),
    )


CORE_INFO_SCRIPT = """import importlib.metadata,json,sys
from pathlib import Path
p=importlib.metadata.distribution('simstack')
d=p.read_text('direct_url.json')
origin=Path(sys.prefix)/'simstack-core-origin.json'
o=json.loads(origin.read_text()) if origin.is_file() else {}
if o and o.get('version') != p.version:
    raise ValueError('Operator Core origin version disagrees with installed metadata')
print(json.dumps({'version':p.version,'direct_url':json.loads(d) if d else None,'commit_id':o.get('commit_id')}))
"""


def validate_platform_core(project: UvProject, platform: dict) -> None:
    """Core stays operator-owned; a project cannot silently select another Core."""
    for package in project.core_packages:
        if package.get("version") != platform.get("version"):
            raise ValueError(
                "uv.lock selects a different SimStack Core version; update the project lock for this API release"
            )
        source = package.get("source", {})
        if "git" in source:
            revision = source["git"].rsplit("#", 1)[-1]
            actual = platform.get("commit_id") or (
                platform.get("direct_url") or {}
            ).get("vcs_info", {}).get("commit_id")
            if not re.fullmatch(r"[0-9a-f]{40}", revision) or actual != revision:
                raise ValueError(
                    "uv.lock selects a different SimStack Core revision; update the project lock for this API release"
                )
        elif "registry" not in source:
            raise ValueError(
                "A workflow cannot replace platform Core with a bundled path dependency"
            )


_UV_EXEC_SCRIPT = """import json,os,sys
settings=json.loads(sys.argv[1])
os.environ.update(settings)
os.execv(sys.argv[2],sys.argv[2:])
"""


_UV_BUILD_PREFETCH_SCRIPT = """import json,os,subprocess,sys
from pathlib import Path
settings=json.loads(sys.argv[1])
os.environ.update(settings)
uv,python,cache=sys.argv[2:5]
for index,requirements in enumerate(json.loads(sys.argv[5])):
    target=Path(cache)/'build-requirements'/str(index)
    target.mkdir(parents=True,exist_ok=True)
    subprocess.run([uv,'--no-config','pip','install','--python',python,
                    '--target',str(target),'--only-binary',':all:',
                    '--cache-dir',cache,*requirements],check=True)
"""


def uv_preparation_commands(
    project: UvProject,
    environment: str | Path,
    base_python: str,
    *,
    uv: str | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, list[str] | None]:
    """Return wheel-only online stages and a separate sandbox-only offline sync.

    The caller supplies a private writable project copy for the offline build;
    the archive's immutable source tree is never modified by packaging backends.
    """
    executable = uv or os.environ.get("SIMSTACK_UV_BINARY") or shutil.which("uv")
    if executable is None or not Path(executable).is_file():
        raise ValueError("uv is required to prepare a uv-locked workflow project")
    executable = str(Path(executable).absolute())
    environment = Path(environment).absolute()
    cache = (
        Path(cache_dir).absolute()
        if cache_dir is not None
        else environment.parent / ".uv-cache"
    )
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = {
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "UV_CACHE_DIR": str(cache),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_CONCURRENT_DOWNLOADS": "4",
        "UV_CONCURRENT_BUILDS": "1",
        "UV_CONCURRENT_INSTALLS": "2",
        "HOME": str(cache / "home"),
        "XDG_CONFIG_HOME": str(cache / "config"),
    }

    def command(arguments):
        return [
            str(base_python),
            "-I",
            "-c",
            _UV_EXEC_SCRIPT,
            json.dumps(settings, sort_keys=True),
            executable,
            *arguments,
        ]

    common = [
        "sync",
        "--frozen",
        "--no-default-groups",
        "--no-editable",
        "--python",
        str(base_python),
        "--project",
        str(project.root),
        "--no-install-package",
        "simstack",
        "--cache-dir",
        str(cache),
    ]
    prefetch = [*common, "--no-build", "--no-install-project", "--inexact"]
    for package_name in project.source_packages:
        prefetch.extend(("--no-install-package", package_name))
    build = None
    groups = project.build_requirement_groups or (
        (project.build_requirements,) if project.build_requirements else ()
    )
    if groups:
        # Backend wheels populate only the private uv cache/targets. They are
        # never added to the workflow interpreter: setuptools' startup hook and
        # mutually incompatible backend versions belong in isolated build envs.
        build = [
            str(base_python),
            "-I",
            "-c",
            _UV_BUILD_PREFETCH_SCRIPT,
            json.dumps(settings, sort_keys=True),
            executable,
            str(base_python),
            str(cache),
            json.dumps(groups),
        ]
    return {
        "prefetch": command(prefetch),
        "build_dependencies": build,
        "sync": command([*common, "--offline", "--inexact"]),
    }
