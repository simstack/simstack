import subprocess
import os
import shlex
import shutil
import sys
import importlib.metadata
import json

import pytest

from simstack.util.repository_runtime import verify_source


def git(root,*arguments):
    return subprocess.check_output(["git","-C",str(root),*arguments],text=True).strip()


def init(root):
    root.mkdir()
    git(root,"init","--quiet")
    git(root,"config","user.name","Fixture")
    git(root,"config","user.email","fixture@example.invalid")


def test_recursive_submodule_pin_and_dirty_sources_are_checked(tmp_path):
    child = tmp_path/"child"
    init(child)
    (child/"code.py").write_text("VALUE=1\n")
    git(child,"add","code.py")
    git(child,"commit","--quiet","-m","Initial")
    child_sha = git(child,"rev-parse","HEAD")
    root = tmp_path/"root"
    init(root)
    git(root,"-c","protocol.file.allow=always","submodule","add","--quiet",str(child),"nested lib")
    git(root,"commit","--quiet","-am","Add source")
    descriptor = {"commit_sha":git(root,"rev-parse","HEAD"),
                  "submodules":[{"path":"nested lib","commit_sha":child_sha}]}
    git(root,"tag","v1")
    descriptor["git_metadata"] = [{"path":".","tags":[{"name":"refs/tags/v1","object_sha":descriptor["commit_sha"]}]},
        {"path":"nested lib","tags":[]}]
    verify_source(root,descriptor)
    git(root,"tag","unrecorded-tag")
    with pytest.raises(RuntimeError,match="tags"):
        verify_source(root,descriptor)
    git(root,"tag","-d","unrecorded-tag")
    (root/"nested lib/code.py").write_text("VALUE=2\n")
    with pytest.raises(RuntimeError,match="modified|changed"):
        verify_source(root,descriptor)
    git(root/"nested lib","checkout","--","code.py")
    verify_source(root,descriptor)
    # Build backends may generate metadata inside a workspace submodule.
    (root / "nested lib" / "generated.egg-info").mkdir()
    (root / "nested lib" / "generated.egg-info" / "PKG-INFO").write_text("generated build metadata")
    verify_source(root, descriptor)
    descriptor["submodules"][0]["commit_sha"]="b"*40
    with pytest.raises(RuntimeError,match="differ"):
        verify_source(root,descriptor)


@pytest.mark.parametrize("url", ["git@custom.example:team/workflow.git", "ssh://git@github.com:2222/org/repo.git",
    "https://gitlab.example/team/repo.git", "ssh://git@[::1]:2222/team/repo.git"])
def test_clone_urls_support_arbitrary_ssh_and_https_hosts(url):
    from simstack.util.repository_runtime import validate_clone_url
    assert validate_clone_url(url) == url


@pytest.mark.parametrize("url", ["/tmp/repo", "file:///tmp/repo", "ext::sh -c command", "--upload-pack=bad",
    "https://token@example.test/repo.git", "ssh://git:password@example.test/repo.git", "https://example.test/",
    "git@example.test:repo.git\n--config=bad", "http://example.test/repo.git", "https://example.test/repo?token=private"])
def test_clone_urls_reject_credentials_options_and_other_transports(url):
    from simstack.util.repository_runtime import validate_clone_url
    with pytest.raises(ValueError):
        validate_clone_url(url)


def test_ssh_environment_uses_installed_key_and_explicit_host_trust(tmp_path, monkeypatch):
    from simstack.util.repository_runtime import git_environment
    key = tmp_path / "private key"
    key.write_text("private-key-bytes")
    key.chmod(0o600)
    hosts = tmp_path / "known hosts"
    hosts.write_text("")
    monkeypatch.setenv("SIMSTACK_GIT_SSH_KEY", str(key))
    monkeypatch.setenv("SIMSTACK_GIT_KNOWN_HOSTS", str(hosts))
    env = git_environment(require_key=True)
    command = shlex.split(env["GIT_SSH_COMMAND"])
    assert str(key) in command
    assert "StrictHostKeyChecking=yes" in command
    assert "UserKnownHostsFile=" + str(hosts) in command
    assert "IdentityAgent=none" in command
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "private-key-bytes" not in env["GIT_SSH_COMMAND"]
    key.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        git_environment(require_key=True)


def _descriptor(root, revision, *, extras=None):
    modules = git(root, "submodule", "foreach", "--quiet", "--recursive",
        "printf '%s\\0%s\\0' \"$displaypath\" \"$(git rev-parse HEAD)\"").split("\0")
    submodules = [{"path": modules[index], "commit_sha": modules[index+1]}
                  for index in range(0, len(modules)-1, 2)]
    metadata = []
    for path in [".", *(item["path"] for item in submodules)]:
        refs = git(root / path, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/tags")
        tags = [dict(zip(("name", "object_sha"), row.split("\0"))) for row in refs.splitlines()]
        metadata.append({"path": path, "tags": tags})
    return {"source_mode": "git", "clone_url": str(root), "repository_revision": revision,
        "commit_sha": git(root, "rev-parse", "HEAD"), "submodules": submodules,
        "git_metadata": metadata, "extras": extras or []}


def test_git_cache_pins_old_commit_submodules_and_tags_and_keeps_versions(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    child, source, cache = tmp_path / "child", tmp_path / "remote", tmp_path / "cache"
    init(child)
    (child / "value.py").write_text("VERSION = 1\n")
    git(child, "add", ".")
    git(child, "commit", "--quiet", "-m", "Child")
    git(child, "tag", "-a", "v1", "-m", "Child release")
    init(source)
    (source / "value.py").write_text("VERSION = 1\n")
    git(source, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(child), "nested lib")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "First")
    git(source, "tag", "-a", "v1", "-m", "First release")
    first = _descriptor(source, "a" * 64, extras=["machine-a"])
    (source / "value.py").write_text("VERSION = 2\n")
    git(source, "commit", "--quiet", "-am", "Second")
    git(source, "tag", "v2")
    second = _descriptor(source, "b" * 64, extras=["machine-b"])
    # A local bare/working Git fixture exercises real Git checkout operations;
    # production validation and allowed transports are tested separately above.
    monkeypatch.setattr(runtime, "validate_clone_url", lambda value: value)
    monkeypatch.setattr(runtime, "git_environment", lambda **kwargs: {**os.environ, "GIT_ALLOW_PROTOCOL": "file"})
    monkeypatch.setattr(runtime, "_download", lambda *args: pytest.fail("Git sources must not use API archives"))
    prepared = []
    def prepare(root, *, extras, **kwargs):
        prepared.append((root, extras))
        (root / ".venv").mkdir()
        (root / ".venv" / "preserved").write_text("old runtime")
    monkeypatch.setattr(runtime, "prepare_environment", prepare)
    first_root = runtime._cache_revision(None, first, cache=cache)
    assert (first_root / "value.py").read_text() == "VERSION = 1\n"
    verify_source(first_root, first)
    assert runtime._cache_revision(None, first, cache=cache) == first_root
    assert len(prepared) == 1
    second_root = runtime._cache_revision(None, second, cache=cache)
    assert second_root != first_root
    assert prepared[-1][1] == ["machine-b"]
    assert (first_root / ".venv" / "preserved").read_text() == "old runtime"
    assert (second_root / "value.py").read_text() == "VERSION = 2\n"
    verify_source(second_root, second)
    (first_root / "value.py").write_text("changed")
    with pytest.raises(RuntimeError, match="modified"):
        runtime._cache_revision(None, first, cache=cache)
    assert (first_root / ".venv" / "preserved").is_file()


def test_failed_git_runtime_preparation_is_retryable_without_touching_ready_version(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / "source"
    init(source)
    (source / "value.py").write_text("VALUE=1\n")
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "First")
    descriptor = _descriptor(source, "c" * 64)
    monkeypatch.setattr(runtime, "validate_clone_url", lambda value: value)
    monkeypatch.setattr(runtime, "git_environment", lambda **kwargs: {**os.environ, "GIT_ALLOW_PROTOCOL": "file"})
    def fail(*args, **kwargs):
        raise RuntimeError("uv failed")
    monkeypatch.setattr(runtime, "prepare_environment", fail)
    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError, match="uv failed"):
        runtime._cache_revision(None, descriptor, cache=cache)
    assert not list(cache.glob("*/.simstack-repository.json"))
    monkeypatch.setattr(runtime, "prepare_environment", lambda *args, **kwargs: None)
    root = runtime._cache_revision(None, descriptor, cache=cache)
    assert (root / ".simstack-repository.json").is_file()


@pytest.mark.skipif(not (os.environ.get("UV_EXECUTABLE") or shutil.which("uv")), reason="uv executable is required")
def test_cold_git_cache_runs_real_locked_uv_sync_and_preserves_installed_core(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / "source"
    init(source)
    (source / "pyproject.toml").write_text(
        '[project]\nname="cold-runner-fixture"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=[]\n'
        '[project.optional-dependencies]\nmachine=[]\n[tool.uv]\npackage=false\n')
    (source / "fixture_workflow.py").write_text("RESULT = 42\n")
    uv = os.environ.get("UV_EXECUTABLE") or shutil.which("uv")
    subprocess.run([uv, "lock", "--python", sys.executable], cwd=source, check=True, capture_output=True)
    git(source, "add", ".")
    git(source, "commit", "--quiet", "-m", "Locked workflow")
    descriptor = _descriptor(source, "d" * 64, extras=["machine"])
    monkeypatch.setattr(runtime, "validate_clone_url", lambda value: value)
    monkeypatch.setattr(runtime, "git_environment", lambda **kwargs: {**os.environ, "GIT_ALLOW_PROTOCOL": "file"})
    root = runtime._cache_revision(None, descriptor, cache=tmp_path / "cache")
    output = subprocess.check_output([str(runtime.runtime_python(root)), "-c",
        "import simstack,fixture_workflow,importlib.metadata;print(fixture_workflow.RESULT);print(importlib.metadata.version('simstack'))"],
        cwd=root, text=True).splitlines()
    assert output == ["42", importlib.metadata.version("simstack")]
    assert (root / "uv.lock").read_bytes() == (source / "uv.lock").read_bytes()
    verify_source(root, descriptor)


def test_startup_inventory_keeps_valid_profiles_without_preparing_missing_or_dirty_sources(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    cache = tmp_path / "cache"
    cache.mkdir()
    for revision in ("a", "b", "c"):
        root = cache / (revision * 64 + "-profile")
        init(root)
        (root / "value.py").write_text("VALUE=1\n")
        git(root, "add", ".")
        git(root, "commit", "--quiet", "-m", "Prepared workflow")
        descriptor = _descriptor(root, revision * 64, extras=["gpu", "cpu", "gpu"])
        (root / ".simstack-repository.json").write_text(json.dumps(descriptor))
        if revision != "b":
            python = runtime.runtime_python(root)
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
        if revision == "c":
            (root / "value.py").write_text("MODIFIED=1\n")
    invalid = cache / "incomplete"
    invalid.mkdir()
    (invalid / ".simstack-repository.json").write_text("not-json")
    monkeypatch.setattr(runtime, "prepare_environment", lambda *args, **kwargs: pytest.fail("Startup must not sync"))
    monkeypatch.setattr(runtime, "_checkout_git", lambda *args, **kwargs: pytest.fail("Startup must not clone"))
    assert runtime.prepared_repositories(cache=cache) == [{"repository_revision": "a" * 64, "extras": ["cpu", "gpu"]}]


@pytest.mark.asyncio
async def test_empty_cache_reconciles_before_any_lazy_checkout(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    monkeypatch.setenv("SIMSTACK_REPOSITORY_CACHE", str(tmp_path / "cold-cache"))
    api = SimpleNamespace(json=AsyncMock())
    await runtime.reconcile_prepared_repositories(api)
    api.json.assert_awaited_once_with("POST", "/runner/repositories/reconcile", {"prepared": []})
    assert not (tmp_path / "cold-cache").exists()
