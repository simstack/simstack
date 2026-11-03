"""Prepare exact Git revisions in an immutable, Runner-local runtime cache."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path


def prepare_environment(source: Path, *, extras: list[str], runtime: Path | None = None,
                        environment: dict[str, str] | None = None) -> Path:
    """Keep uv's locked project/workspace behavior and the installed platform Core."""
    runtime = runtime or source / ".venv"
    uv = os.environ.get("UV_EXECUTABLE") or shutil.which("uv") or str(Path.home()/".local/bin/uv")
    env = dict(os.environ if environment is None else environment)
    # uv materializes Git dependencies by cloning its own local bare cache.
    # Root/submodule checkout still receives the stricter SSH/HTTPS environment.
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
    env["UV_PROJECT_ENVIRONMENT"] = str(runtime)
    env["UV_NO_PROGRESS"] = "1"
    if (source/"pyproject.toml").is_file():
        command = [uv, "sync", "--locked", "--python", sys.executable, "--no-install-package", "simstack"]
        for extra in extras:
            command += ["--extra", extra]
    else:
        command = [uv, "venv", "--system-site-packages", "--python", sys.executable, str(runtime)]
    result = subprocess.run(command, cwd=source, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=1200)
    if result.returncode:
        raise RuntimeError("Repository dependency preparation failed; check uv.lock, extras, Python and native libraries")
    python = runtime/("Scripts/python.exe" if os.name == "nt" else "bin/python")
    sites = json.loads(subprocess.check_output([str(python), "-c", "import json,site;print(json.dumps(site.getsitepackages()))"],
                                               cwd=source, env=env))
    # Root projects may pin an older SimStack. Execution always uses this Runner's
    # platform Core; all workflow dependencies still come from the project's lock.
    import simstack
    package = Path(simstack.__file__).resolve().parent
    platform = runtime/".simstack-platform"
    platform.mkdir(exist_ok=True)
    link = platform/"simstack"
    if not link.exists():
        if os.name == "nt":
            shutil.copytree(package, link)
        else:
            link.symlink_to(package, target_is_directory=True)
    distribution = importlib.metadata.distribution("simstack")
    metadata = Path(distribution._path)
    if metadata.exists() and not (platform/metadata.name).exists():
        if os.name == "nt":
            shutil.copytree(metadata, platform/metadata.name)
        else:
            (platform/metadata.name).symlink_to(metadata, target_is_directory=True)
    Path(sites[0], "simstack-platform.pth").write_text(str(platform)+"\n")
    return python


def validate_clone_url(value: str) -> str:
    """Accept Git's SSH and HTTPS clone syntax without accepting local transports."""
    if not isinstance(value, str) or not value or value != value.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("Repository clone URL must use SSH or HTTPS")
    if "://" in value:
        try:
            parsed = urllib.parse.urlsplit(value)
            valid = (parsed.scheme in {"ssh", "https"} and parsed.hostname
                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*|[0-9A-Fa-f:]+", parsed.hostname)
                     and parsed.path not in {"", "/"} and parsed.password is None
                     and not parsed.query and not parsed.fragment)
            if parsed.scheme == "https" and parsed.username is not None:
                valid = False
            if parsed.username and not re.fullmatch(r"[A-Za-z0-9_.-]+", parsed.username):
                valid = False
            _ = parsed.port
        except ValueError:
            valid = False
        if valid:
            return value
    elif re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9A-Fa-f:]+\]):[^\s?#]+", value):
        return value
    raise ValueError("Repository clone URL must use SSH or HTTPS without embedded credentials")


def git_environment(*, require_key: bool = False) -> dict[str, str]:
    """Use the enrolled identity and explicitly trusted host keys for Git and uv."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_SSH_VARIANT"] = "ssh"
    key = env.get("SIMSTACK_GIT_SSH_KEY")
    hosts = env.get("SIMSTACK_GIT_KNOWN_HOSTS")
    if require_key and not key:
        raise RuntimeError("SSH key is not installed; rerun the Runner installer")
    command = ["ssh", "-F", os.devnull, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
               "-o", "GlobalKnownHostsFile=" + os.devnull]
    if key:
        path = Path(key).expanduser().resolve()
        if not path.is_file() or (os.name != "nt" and path.stat().st_mode & 0o077):
            raise RuntimeError("Runner SSH key is missing or its permissions are not private (0600)")
        command += ["-i", str(path)]
    else:
        command += ["-o", "IdentityFile=none"]
    command += ["-o", "UserKnownHostsFile=" + (str(Path(hosts).expanduser().resolve()) if hosts else os.devnull)]
    env["GIT_SSH_COMMAND"] = shlex.join(command)
    return env


def _git(root: Path, *arguments: str, environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Repository Git checkout failed; check Git installation and network access") from None
    if result.returncode:
        # Git's output may include remote URLs and external credential helper
        # diagnostics. Only fixed, safe text crosses the Runner control API.
        raise RuntimeError("Repository Git checkout failed; check SSH host trust, repository access and pinned revisions")
    return result.stdout


def _checkout_git(source: Path, descriptor: dict, environment: dict[str, str]):
    url = validate_clone_url(descriptor["clone_url"])
    commit = descriptor["commit_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Invalid pinned Git commit")
    _git(source.parent, "clone", "--no-checkout", "--no-tags", "--", url, str(source), environment=environment)
    _git(source, "fetch", "--no-tags", "origin", commit, environment=environment)
    _git(source, "checkout", "--detach", commit, environment=environment)
    _git(source, "submodule", "update", "--init", "--recursive", environment=environment)
    for metadata in descriptor.get("git_metadata", []):
        repository = (source / metadata["path"]).resolve()
        if not repository.is_relative_to(source.resolve()):
            raise RuntimeError("Git metadata is outside the repository cache")
        # Tags are part of the published package version (e.g. setuptools-scm).
        # A later tag on the remote must not silently change an old runtime.
        existing = _git(repository, "for-each-ref", "--format=%(refname)", "refs/tags", environment=environment)
        for tag in existing.splitlines():
            _git(repository, "update-ref", "-d", tag, environment=environment)
        for tag in metadata["tags"]:
            name, sha = tag["name"], tag["object_sha"]
            if not name.startswith("refs/tags/") or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                raise ValueError("Invalid pinned Git tag")
            _git(repository, "check-ref-format", name, environment=environment)
            _git(repository, "fetch", "--no-tags", "origin", sha, environment=environment)
            _git(repository, "update-ref", name, sha, environment=environment)


def _download(client, path: str, destination: Path):
    request = urllib.request.Request(client.server_url.rstrip("/") + "/" + path.lstrip("/"),
        headers={"Authorization": "Bearer "+client.token})
    with urllib.request.urlopen(request, timeout=600) as response, destination.open("wb") as output:
        digest = hashlib.sha256()
        while chunk := response.read(1024*1024):
            digest.update(chunk)
            output.write(chunk)
        expected = response.headers.get("X-Repository-SHA256")
        if not expected or digest.hexdigest() != expected:
            raise RuntimeError("Repository source download checksum mismatch")


def verify_source(root: Path, descriptor: dict):
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(root), *arguments], text=True)
    if git("rev-parse", "HEAD").strip() != descriptor["commit_sha"]:
        raise RuntimeError("Cached repository commit differs from the pinned revision")
    if subprocess.run(["git", "-C", str(root), "diff", "--ignore-submodules=untracked", "--quiet", "HEAD", "--"], check=False).returncode:
        raise RuntimeError("Cached repository source was modified")
    statuses = git("submodule", "status", "--recursive")
    if any(line and line[0] != " " for line in statuses.splitlines()):
        raise RuntimeError("Cached recursive submodule checkout was changed")
    graph = git("submodule", "foreach", "--quiet", "--recursive",
        "printf '%s\\0%s\\0' \"$displaypath\" \"$(git rev-parse HEAD)\"")
    values = graph.split("\0")
    actual = [{"path": values[index], "commit_sha": values[index+1]}
              for index in range(0, len(values)-1, 2)]
    if actual != descriptor["submodules"]:
        raise RuntimeError("Cached recursive submodule revisions differ from the task pin")
    for metadata in descriptor.get("git_metadata", []):
        repository = (root/metadata["path"]).resolve()
        if not repository.is_relative_to(root.resolve()):
            raise RuntimeError("Git metadata is outside the repository cache")
        refs = subprocess.check_output(["git", "-C", str(repository), "for-each-ref",
            "--format=%(refname)%00%(objectname)", "refs/tags"], text=True)
        tags = [dict(zip(("name", "object_sha"), row.split("\0"))) for row in refs.splitlines()]
        if tags != metadata["tags"]:
            raise RuntimeError("Cached Git tags differ from the pinned package version")
    for item in actual:
        child = (root/item["path"]).resolve()
        if not child.is_relative_to(root.resolve()):
            raise RuntimeError("Submodule is outside the repository cache")
        if subprocess.run(["git", "-C", str(child), "diff", "--ignore-submodules=untracked", "--quiet", "HEAD", "--"], check=False).returncode:
            raise RuntimeError("Cached recursive submodule source was modified")


def runtime_python(root: Path) -> Path:
    return root/".venv"/("Scripts/python.exe" if os.name == "nt" else "bin/python")


@contextlib.contextmanager
def cache_lock(path: Path):
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic()+1800
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Repository cache is busy")
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield


def _cache_revision(client, descriptor, *, cache: Path) -> Path:
    revision = descriptor["repository_revision"]
    if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Invalid repository revision")
    cache.mkdir(parents=True, exist_ok=True)
    profile = hashlib.sha256(json.dumps(descriptor.get("extras", []), sort_keys=True).encode()).hexdigest()[:12]
    key = revision+"-"+profile
    root = cache/key
    lock_file = cache/(key+".lock")
    with cache_lock(lock_file):
        marker = root/".simstack-repository.json"
        expected = {"repository_revision": revision, "commit_sha": descriptor["commit_sha"], "submodules": descriptor["submodules"],
                    "extras": descriptor.get("extras", []), "git_metadata": descriptor.get("git_metadata", [])}
        if root.exists():
            if not marker.is_file() or json.loads(marker.read_text()) != expected:
                raise RuntimeError("Repository runtime cache differs from its pin; retain it until its tasks finish")
            verify_source(root, descriptor)
            return root
        url = descriptor.get("clone_url")
        from_git = descriptor.get("source_mode") == "git" or bool(url)
        if from_git:
            validate_clone_url(url)
        environment = git_environment(require_key=bool(from_git and not url.startswith("https://")))
        with tempfile.TemporaryDirectory(prefix="source-", dir=cache) as temporary:
            staging = Path(temporary)
            archive = staging/"repository.tar.gz"
            source = staging/"source"
            if from_git:
                _checkout_git(source, descriptor, environment)
            else:
                source.mkdir()
                _download(client, descriptor["archive_url"], archive)
                with tarfile.open(archive, "r:gz") as package:
                    package.extractall(source, filter="data")
            verify_source(source, descriptor)
            # Virtual environments contain absolute paths, so prepare at the final location.
            source.rename(root)
            try:
                shutil.rmtree(root/".venv", ignore_errors=True)
                prepare_environment(root, extras=descriptor.get("extras", []), environment=environment)
                verify_source(root, descriptor)
                (root/".simstack-repository.json").write_text(json.dumps(expected, sort_keys=True))
            except BaseException:
                # This directory was never published as ready. Existing pinned
                # runtimes are untouched, and a failed preparation can retry.
                shutil.rmtree(root, ignore_errors=True)
                raise
        return root


def repository_cache() -> Path:
    return Path(os.environ.get("SIMSTACK_REPOSITORY_CACHE",
        str(Path.home()/".local/share/simstack/repositories")))


def prepared_repositories(*, cache: Path | None = None) -> list[dict]:
    """Reconcile readiness from verified local runtimes before admitting tasks.

    This performs local verification only. A cold/reinstalled Runner must let
    the Server's normal busy-gated control path prepare any missing version.
    """
    cache = repository_cache() if cache is None else cache
    inventory = []
    for marker in sorted(cache.glob("*/.simstack-repository.json")):
        root = marker.parent
        if root.is_symlink():
            continue
        try:
            value = json.loads(marker.read_text())
            revision, extras = value["repository_revision"], value.get("extras", [])
            if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision)
                    or not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras)
                    or not runtime_python(root).is_file() or not os.access(runtime_python(root), os.X_OK)):
                continue
            verify_source(root, value)
            inventory.append({"repository_revision": revision, "extras": sorted(set(extras))})
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            continue
    return inventory


async def reconcile_prepared_repositories(client):
    inventory = await asyncio.to_thread(prepared_repositories)
    await client.json("POST", "/runner/repositories/reconcile", {"prepared": inventory})


async def prepare_repository(client, descriptor: dict) -> Path:
    return await asyncio.to_thread(_cache_revision, client, descriptor, cache=repository_cache())


async def prepare_task_runtime(entry) -> Path | None:
    revision = getattr(entry, "repository_revision", None)
    if not revision:
        return None
    mounted = os.environ.get("SIMSTACK_REPOSITORY_ROOT")
    if mounted:
        root = Path(mounted).resolve(strict=True)
        marker = root/".simstack-repository.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("repository_revision") != revision:
            raise RuntimeError("Mounted repository does not match the task revision")
        return root
    from simstack.runner_api.transport import RunnerAPIClient
    client = RunnerAPIClient.from_environment()
    descriptor = await client.json("GET", "/runner/repositories/"+revision)
    return await prepare_repository(client, descriptor)
