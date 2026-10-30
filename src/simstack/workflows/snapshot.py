"""Build portable project ZIPs while uv retains ownership of dependency locks.

This command runs explicitly on a project author's machine. It never installs
dependencies or executes workflow nodes. uv itself verifies and relocates the
lock; SimStack records source provenance rather than resolving packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SNAPSHOT_PATH = Path(".simstack/snapshot.json")
_OMIT = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    ".ssh",
    ".aws",
    ".kube",
    ".docker",
    "dist",
    "build",
}
_LOCAL_CONFIGS = {"config.toml", "simstack.toml", "runner.toml"}
_NATIVE_SUFFIXES = {".so", ".dylib", ".dll", ".exe", ".o", ".a", ".sif"}


def _run(command, *, cwd=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # A Git/uv error can contain credential-bearing URLs. Keep logs private.
        raise ValueError(
            f"{Path(command[0]).name} failed while preparing the locked snapshot"
        )
    return result.stdout


def _check_credentials(text):
    for value in re.findall(r"(?:https?|ssh)://[^\s\"'<>]+", text):
        url = urlsplit(value)
        if url.password is not None or (
            url.scheme in {"http", "https"} and url.username is not None
        ):
            raise ValueError("Credentials must remain outside workflow source metadata")


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _toml(path):
    text = path.read_text(encoding="utf-8")
    _check_credentials(text)
    return tomllib.loads(text)


def _files(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                f"Source snapshots cannot contain symlinks: {path.relative_to(root)}"
            )
        if path.is_file():
            yield path


def tree_digest(root: Path) -> str:
    """Digest names and bytes, independent of filesystem timestamps or modes."""
    root = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _contained(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Snapshot source path must be a relative POSIX path")
    candidate = Path(relative)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.split("/")
    ):
        raise ValueError("Snapshot source path escapes the archive")
    result = (root / candidate).resolve(strict=True)
    if not result.is_relative_to(root) or not result.is_dir():
        raise ValueError("Snapshot source path escapes the archive")
    return result


def read_snapshot(root: str | Path) -> dict | None:
    """Validate every bundled source before it can participate in installation."""
    root = Path(root).resolve(strict=True)
    metadata = root / SNAPSHOT_PATH
    if not metadata.exists():
        return None
    if metadata.is_symlink() or metadata.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Invalid source snapshot metadata")
    value = json.loads(metadata.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("sources"), list)
    ):
        raise ValueError("Unsupported source snapshot metadata")
    names = set()
    for source in value["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("name"), str):
            raise ValueError("Invalid bundled source declaration")  # noqa: TRY004 -- malformed persisted JSON
        name = canonicalize_name(source["name"])
        if name != source["name"] or name in names or name == "simstack":
            raise ValueError("Conflicting or protected bundled source")
        names.add(name)
        path = _contained(root, source.get("path"))
        if not isinstance(source.get("version"), str) or not isinstance(
            source.get("source"), dict
        ):
            raise ValueError("Bundled sources require version and origin")  # noqa: TRY004 -- malformed persisted JSON
        if not re.fullmatch(r"[0-9a-f]{64}", source.get("tree_sha256", "")):
            raise ValueError("Bundled sources require an immutable tree digest")
        if tree_digest(path) != source["tree_sha256"]:
            raise ValueError(f"Bundled source tree changed: {name}")
        project = _toml(path / "pyproject.toml").get("project", {})
        if canonicalize_name(project.get("name", "")) != name:
            raise ValueError("Bundled source does not match its declared package")
    return value


def _excluded(relative):
    return (
        any(
            part in _OMIT or part.endswith((".egg-info", ".dist-info"))
            for part in relative.parts
        )
        or any(part == ".env" or part.startswith(".env.") for part in relative.parts)
        or (len(relative.parts) == 1 and relative.name in _LOCAL_CONFIGS)
    )


def _copy_tree(source, target, *, skip=()):
    source = source.resolve(strict=True)
    target.mkdir(parents=True, exist_ok=True)
    inventory = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    selected = (
        set(inventory.stdout.decode().split("\0"))
        if inventory.returncode == 0
        else None
    )
    for directory, dirs, files in os.walk(source):
        directory = Path(directory)
        relative = directory.relative_to(source)
        dirs[:] = sorted(
            name
            for name in dirs
            if not _excluded(relative / name)
            and (relative / name).as_posix() not in skip
        )
        for name in dirs:
            if (directory / name).is_symlink():
                raise ValueError("Project source directories must not be symlinks")
        for name in sorted(files):
            path, rel = directory / name, relative / name
            if (
                _excluded(rel)
                or rel.as_posix() in skip
                or (selected is not None and rel.as_posix() not in selected)
            ):
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Project source must be a regular file: {rel}")
            if path.suffix.lower() in _NATIVE_SUFFIXES:
                raise ValueError(
                    f"Native binaries belong to resource execution, not source ZIPs: {rel}"
                )
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            destination.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def _git(command, *, cwd=None):
    return _run(["git", *command], cwd=cwd).strip()


def _source_git_url(value):
    url = urlsplit(value)
    if url.scheme not in {"https", "http", "ssh", "file"}:
        raise ValueError("Locked Git sources must use an explicit supported URL")
    if url.scheme in {"http", "https"} and (
        url.username is not None or url.password is not None
    ):
        raise ValueError("Credentials must not be embedded in a source snapshot URL")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", url.fragment):
        raise ValueError("Git sources require an exact commit in uv.lock")
    return urlunsplit((url.scheme, url.netloc, url.path, "", "")), url.fragment.lower()


def _checkout(url, revision, target):
    target.mkdir(parents=True)
    _git(["init", "--quiet", str(target)])
    _git(["remote", "add", "origin", url], cwd=target)
    _git(["fetch", "--quiet", "--depth=1", "origin", revision], cwd=target)
    if _git(["rev-parse", "FETCH_HEAD"], cwd=target) != revision:
        raise ValueError("Git did not return the locked source commit")
    _git(["checkout", "--quiet", "--detach", revision], cwd=target)
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--depth=1",
        ],
        cwd=target,
    )


def _gitlinks(source):
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        if (source / ".gitmodules").exists():
            raise ValueError(
                "Submodule snapshots require a Git checkout with pinned gitlinks"
            )
        return []
    links = []
    for record in result.stdout.split(b"\0"):
        if record.startswith(b"160000 "):
            metadata, path = record.split(b"\t", 1)
            links.append((path.decode(), metadata.split()[1].decode()))
    return links


def _copy_project(source, target, temporary):
    if (source / ".gitmodules").is_file():
        _check_credentials((source / ".gitmodules").read_text())
    links = _gitlinks(source)
    _copy_tree(source, target, skip={path for path, _ in links})
    origins = []
    for index, (relative, revision) in enumerate(links):
        existing = source / relative
        if (existing / ".git").exists():
            if _git(["rev-parse", "HEAD"], cwd=existing) != revision or _git(
                ["status", "--porcelain"], cwd=existing
            ):
                raise ValueError(
                    f"Submodule must match its pinned clean commit: {relative}"
                )
            checkout = existing
        else:
            module_path = _git(
                [
                    "config",
                    "-f",
                    ".gitmodules",
                    "--get-regexp",
                    r"^submodule\..*\.path$",
                ],
                cwd=source,
            )
            candidates = [
                line.split(None, 1)[0]
                for line in module_path.splitlines()
                if line.split(None, 1)[1] == relative
            ]
            if len(candidates) != 1:
                raise ValueError("Submodule path has no unique configured source")
            url = _git(
                ["config", "-f", ".gitmodules", "--get", candidates[0][:-4] + "url"],
                cwd=source,
            )
            if url.startswith(("./", "../")):
                origin = _git(["remote", "get-url", "origin"], cwd=source)
                if "://" not in origin:
                    raise ValueError(
                        "Relative submodule URLs require an absolute origin URL"
                    )
                url = urljoin(origin.rstrip("/") + "/", url)
            checkout = temporary / f"submodule-{index}"
            _checkout(url, revision, checkout)
        nested = _copy_project(
            checkout, target / relative, temporary / f"nested-{index}"
        )
        origins.append({"path": relative, "commit": revision})
        origins.extend(
            {"path": relative + "/" + entry["path"], "commit": entry["commit"]}
            for entry in nested
        )
    return origins


def _rewrite_requirements(values, versions):
    result = []
    for value in values:
        if isinstance(value, dict):
            result.append(value)
            continue
        requirement = Requirement(value)
        name = canonicalize_name(requirement.name)
        if requirement.url and name in versions:
            extra = (
                "[" + ",".join(sorted(requirement.extras)) + "]"
                if requirement.extras
                else ""
            )
            value = requirement.name + extra + "==" + versions[name]
            if requirement.marker:
                value += "; " + str(requirement.marker)
        result.append(value)
    return result


def _write_relocated_project(path, sources, versions):
    import tomli_w

    value = _toml(path)
    project = value.get("project", {})
    if "dependencies" in project:
        project["dependencies"] = _rewrite_requirements(
            project["dependencies"], versions
        )
    for extra, requirements in project.get("optional-dependencies", {}).items():
        project["optional-dependencies"][extra] = _rewrite_requirements(
            requirements, versions
        )
    for group, requirements in value.get("dependency-groups", {}).items():
        value["dependency-groups"][group] = _rewrite_requirements(
            requirements, versions
        )
    uv = value.setdefault("tool", {}).setdefault("uv", {})
    for field in ("override-dependencies", "constraint-dependencies"):
        if field in uv:
            uv[field] = _rewrite_requirements(uv[field], versions)
    mappings = uv.setdefault("sources", {})
    for key in list(mappings):
        if canonicalize_name(key) in sources:
            del mappings[key]
    for name, target in sources.items():
        mappings[name] = {
            "path": os.path.relpath(target, path.parent).replace(os.sep, "/")
        }
    path.write_text(tomli_w.dumps(value), encoding="utf-8")


def _graph(lock):
    def package_key(package):
        source = package.get("source", {})
        immutable = (
            {
                "source": source,
                "wheels": package.get("wheels"),
                "sdist": package.get("sdist"),
            }
            if "registry" in source or canonicalize_name(package["name"]) == "simstack"
            else None
        )
        return (
            canonicalize_name(package["name"]),
            package["version"],
            tuple(sorted(package.get("resolution-markers", []))),
            json.dumps(immutable, sort_keys=True),
        )

    def dependencies(items):
        return sorted(
            (
                canonicalize_name(item["name"]),
                item.get("version"),
                item.get("marker"),
                tuple(item.get("extra", [])),
            )
            for item in items
        )

    packages = sorted(
        (
            package_key(package),
            dependencies(package.get("dependencies", [])),
            sorted(
                (key, dependencies(items))
                for key, items in package.get("optional-dependencies", {}).items()
            ),
            sorted(
                (key, dependencies(items))
                for key, items in package.get("dev-dependencies", {}).items()
            ),
        )
        for package in lock.get("package", [])
    )
    return (
        lock.get("requires-python"),
        sorted(lock.get("resolution-markers", [])),
        packages,
    )


def build_snapshot(
    source: str | Path, output: str | Path, *, uv="uv", python: str | None = None
) -> dict:
    source = Path(source).resolve(strict=True)
    output = Path(output).absolute()
    if not source.is_dir() or not (source / "simstack-workflow.toml").is_file():
        raise ValueError(
            "A project snapshot requires simstack-workflow.toml at its root"
        )
    if (source / SNAPSHOT_PATH).exists():
        raise ValueError("This directory is already a prepared source snapshot")
    original_project, original_lock = source / "pyproject.toml", source / "uv.lock"
    project, lock = _toml(original_project), _toml(original_lock)
    uv_prefix = [str(uv), "--no-progress", "--color", "never"]
    python_option = ["--python", python] if python else []
    _run([*uv_prefix, "lock", "--check", *python_option], cwd=source)
    with tempfile.TemporaryDirectory(prefix="simstack-snapshot-") as scratch:
        scratch = Path(scratch)
        root = scratch / "source"
        submodules = _copy_project(source, root, scratch / "git")
        original = root / ".simstack/original"
        original.mkdir(parents=True)
        shutil.copyfile(original_project, original / "pyproject.toml")
        shutil.copyfile(original_lock, original / "uv.lock")
        entries, relocated, versions = [], {}, {}
        project_name = canonicalize_name(project.get("project", {}).get("name", ""))
        for index, package in enumerate(lock.get("package", [])):
            name, origin = canonicalize_name(package["name"]), package.get("source", {})
            if name in {project_name, "simstack"} or "registry" in origin:
                continue
            if name in relocated:
                raise ValueError(
                    "A source package has multiple platform-specific origins; use distinct compatible source snapshots"
                )
            if "git" in origin:
                url, revision = _source_git_url(origin["git"])
                checkout = scratch / f"dependency-{index}"
                _checkout(url, revision, checkout)
                relative = Path(".simstack/sources") / (name + "-" + revision[:12])
                _copy_project(
                    checkout, root / relative, scratch / f"nested-dependency-{index}"
                )
            elif any(key in origin for key in ("directory", "editable", "virtual")):
                location = next(
                    origin[key]
                    for key in ("directory", "editable", "virtual")
                    if key in origin
                )
                path = (source / location).resolve(strict=True)
                if path == source:
                    continue
                if path.is_relative_to(source):
                    relative = path.relative_to(source)
                    if not (root / relative).is_dir():
                        raise ValueError(
                            "A contained source dependency was excluded from the snapshot"
                        )
                else:
                    relative = Path(".simstack/sources") / name
                    _copy_project(
                        path, root / relative, scratch / f"path-dependency-{index}"
                    )
            else:
                raise ValueError(
                    f"Unsupported uv source for {name}; package index, Git, and local sources are supported"
                )
            relocated[name] = root / relative
            versions[name] = package["version"]
            entries.append(
                {
                    "name": name,
                    "version": package["version"],
                    "path": relative.as_posix(),
                    "source": origin,
                }
            )
        for path in [
            root / "pyproject.toml",
            *(directory / "pyproject.toml" for directory in relocated.values()),
        ]:
            _write_relocated_project(path, relocated, versions)
        _run([*uv_prefix, "lock", *python_option], cwd=root)
        prepared_lock = _toml(root / "uv.lock")
        if _graph(prepared_lock) != _graph(lock):
            raise ValueError(
                "Relocating sources changed the uv dependency graph; update the original lock explicitly first"
            )
        metadata = {
            "version": 1,
            "sources": entries,
            "submodules": submodules,
            "original_project_sha256": hashlib.sha256(
                original_project.read_bytes()
            ).hexdigest(),
            "original_lock_sha256": hashlib.sha256(
                original_lock.read_bytes()
            ).hexdigest(),
        }
        for entry in entries:
            entry["tree_sha256"] = tree_digest(root / entry["path"])
        (root / SNAPSHOT_PATH).write_text(
            json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        read_snapshot(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=output.name + ".", suffix=".tmp", dir=output.parent
        )
        os.close(descriptor)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for path in _files(root):
                    item = zipfile.ZipInfo(
                        path.relative_to(root).as_posix(),
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    item.create_system = 3
                    item.external_attr = (
                        stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)
                    ) << 16
                    item.compress_type = zipfile.ZIP_DEFLATED
                    with (
                        path.open("rb") as stream,
                        archive.open(item, "w") as destination,
                    ):
                        shutil.copyfileobj(stream, destination, length=1024 * 1024)
            from simstack.workflows.archive import validate_archive

            validate_archive(Path(temporary))
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "archive": str(output),
        "sha256": _file_digest(output),
        "sources": [
            {key: entry[key] for key in ("name", "version", "path")}
            for entry in entries
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--python")
    options = parser.parse_args()
    try:
        result = build_snapshot(
            options.source, options.output, uv=options.uv, python=options.python
        )
    except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
