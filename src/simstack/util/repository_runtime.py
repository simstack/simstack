"""Fetch exact repository revisions through the API and retain a Runner-local cache."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


def prepare_environment(source: Path, *, extras: list[str], runtime: Path | None = None) -> Path:
    """Keep uv's locked project/workspace behavior and the installed platform Core."""
    runtime = runtime or source / ".venv"
    uv = os.environ.get("UV_EXECUTABLE") or shutil.which("uv") or str(Path.home()/".local/bin/uv")
    env = dict(os.environ)
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
    if subprocess.run(["git", "-C", str(root), "diff", "--ignore-submodules=none", "--quiet", "HEAD", "--"], check=False).returncode:
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
        if subprocess.run(["git", "-C", str(child), "diff", "--ignore-submodules=none", "--quiet", "HEAD", "--"], check=False).returncode:
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
        if marker.is_file() and json.loads(marker.read_text()) == expected:
            verify_source(root, descriptor)
            return root
        with tempfile.TemporaryDirectory(prefix="source-", dir=cache) as temporary:
            staging = Path(temporary)
            archive = staging/"repository.tar.gz"
            source = staging/"source"
            source.mkdir()
            _download(client, descriptor["archive_url"], archive)
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(source, filter="data")
            verify_source(source, descriptor)
            if root.exists():
                shutil.rmtree(root)
            # Virtual environments contain absolute paths, so prepare at the final location.
            source.rename(root)
            shutil.rmtree(root/".venv", ignore_errors=True)
            prepare_environment(root, extras=descriptor.get("extras", []))
            (root/".simstack-repository.json").write_text(json.dumps(expected, sort_keys=True))
        return root


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
    cache = Path(os.environ.get("SIMSTACK_REPOSITORY_CACHE",
        str(Path.home()/".local/share/simstack/repositories")))
    return await asyncio.to_thread(_cache_revision, client, descriptor, cache=cache)
