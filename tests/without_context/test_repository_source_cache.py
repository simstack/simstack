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


def test_core_build_identity_includes_the_executable_checkpoint_wrapper(tmp_path, monkeypatch):
    from simstack.runner_runtime import core_build
    package = tmp_path / "simstack"
    (package / "runner_runtime").mkdir(parents=True)
    (package / "static").mkdir()
    source = package / "runner_runtime" / "core_build.py"
    source.write_text("PROTOCOL_VERSION = 3\n")
    wrapper = package / "static" / "slurm_checkpoint.sh"
    wrapper.write_text("exit 0\n")
    monkeypatch.setattr(core_build, "__file__", str(source))
    core_build.core_build_identity.cache_clear()
    first = core_build.core_build_identity()
    wrapper.write_text("exit 17\n")
    core_build.core_build_identity.cache_clear()
    assert core_build.core_build_identity() != first
    core_build.core_build_identity.cache_clear()


def init(root):
    root.mkdir()
    git(root,"init","--quiet")
    git(root,"config","user.name","Fixture")
    git(root,"config","user.email","fixture@example.invalid")
    (root / "pyproject.toml").write_text("[tool.simstack]\nrunner-protocol = 3\n")
    git(root, "add", "pyproject.toml")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["user", "local"])
async def test_standalone_pinned_runtime_verifies_current_sdk_source_without_prepared_cache(monkeypatch, tmp_path, authorization):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from simstack.core.context import context
    from simstack.util import repository_runtime as runtime
    root = tmp_path / "caller-source"
    init(root)
    (root / "workflow.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "Pin")
    revision = "a" * 64
    descriptor = _descriptor(root, revision)
    api = SimpleNamespace(requests=SimpleNamespace(authorization=authorization),
        resource=SimpleNamespace(repository_revision=AsyncMock(return_value=descriptor)))
    monkeypatch.setattr(context, "_server", api)
    monkeypatch.setattr(context, "_config", SimpleNamespace(project_root=root))
    monkeypatch.delenv("SIMSTACK_REPOSITORY_ROOT", raising=False)
    prepare = AsyncMock(side_effect=AssertionError("standalone runtime used Runner cache"))
    monkeypatch.setattr(runtime, "prepare_repository", prepare)
    assert await runtime.prepare_task_runtime(SimpleNamespace(repository_revision=revision)) == root
    prepare.assert_not_awaited()
    api.resource.repository_revision.assert_awaited_once_with(revision)
    (root / "workflow.py").write_text("VALUE = 2\n")
    with pytest.raises(RuntimeError, match="modified|changed"):
        await runtime.prepare_task_runtime(SimpleNamespace(repository_revision=revision))


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
    monkeypatch.setattr(runtime, "verify_runtime_core", lambda _root: None)
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
    monkeypatch.setattr(runtime, "verify_runtime_core", lambda _root: None)
    root = runtime._cache_revision(None, descriptor, cache=cache)
    assert (root / ".simstack-repository.json").is_file()


@pytest.mark.skipif(not (os.environ.get("UV_EXECUTABLE") or shutil.which("uv")), reason="uv executable is required")
def test_cold_git_cache_runs_real_locked_uv_sync_and_preserves_installed_core(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / "source"
    init(source)
    (source / "pyproject.toml").write_text(
        '[project]\nname="cold-runner-fixture"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=[]\n'
        '[project.optional-dependencies]\nmachine=[]\n[tool.uv]\npackage=false\n[tool.simstack]\nrunner-protocol=3\n')
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
        descriptor["core_build"] = runtime.core_build_identity()
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
    monkeypatch.setattr(runtime, "verify_runtime_core", lambda _root: None)
    assert runtime.prepared_repositories(cache=cache) == [{"repository_revision": "a" * 64, "extras": ["cpu", "gpu"]}]


@pytest.mark.asyncio
async def test_empty_cache_reconciles_before_any_lazy_checkout(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    monkeypatch.setenv("SIMSTACK_REPOSITORY_CACHE", str(tmp_path / "cold-cache"))
    api = SimpleNamespace(reconcile_repositories=AsyncMock())
    await runtime.reconcile_prepared_repositories(api)
    api.reconcile_repositories.assert_awaited_once_with([])
    assert not (tmp_path / "cold-cache").exists()


def _create_runtime_for_core_probe(root, *, core_parent=None, **_kwargs):
    """A real Python environment; no uv or dependency downloads are needed."""
    import venv
    import simstack
    from pathlib import Path
    from simstack.util.repository_runtime import runtime_python
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(root / '.venv')
    python = runtime_python(root)
    sites = json.loads(subprocess.check_output([str(python), '-I', '-c',
        'import json,site; print(json.dumps(site.getsitepackages()))'], text=True))
    Path(sites[0], 'simstack-platform.pth').write_text(str(core_parent or Path(simstack.__file__).resolve().parent.parent) + '\n')


def test_core_upgrade_prepares_separate_runtime_and_never_changes_old_one(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / 'source'
    init(source)
    (source / 'value.py').write_text('VALUE=1\n')
    git(source, 'add', '.')
    git(source, 'commit', '--quiet', '-m', 'Pinned scientific code')
    descriptor = _descriptor(source, 'e' * 64)
    monkeypatch.setattr(runtime, 'validate_clone_url', lambda value: value)
    monkeypatch.setattr(runtime, 'git_environment', lambda **kwargs: {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'})
    def prepare(root, **kwargs):
        (root / '.venv').mkdir()
        (root / '.venv' / 'scientific-dependencies').write_text('pinned dependencies')
    monkeypatch.setattr(runtime, 'prepare_environment', prepare)
    monkeypatch.setattr(runtime, 'verify_runtime_core', lambda root: None)
    monkeypatch.setattr(runtime, 'core_build_identity', lambda: '1' * 64)
    cache = tmp_path / 'cache'
    old_root = runtime._cache_revision(None, descriptor, cache=cache)
    old_marker = (old_root / '.simstack-repository.json').read_bytes()
    monkeypatch.setattr(runtime, 'core_build_identity', lambda: '2' * 64)
    assert runtime.prepared_repositories(cache=cache) == []
    new_root = runtime._cache_revision(None, descriptor, cache=cache)
    assert new_root != old_root
    assert (old_root / '.simstack-repository.json').read_bytes() == old_marker
    assert (old_root / '.venv' / 'scientific-dependencies').read_text() == 'pinned dependencies'
    assert git(new_root, 'rev-parse', 'HEAD') == descriptor['commit_sha']
    assert json.loads((new_root / '.simstack-repository.json').read_text())['core_build'] == '2' * 64


def test_real_interpreter_rejects_stale_copied_core_binding(tmp_path):
    from pathlib import Path
    from simstack.util import repository_runtime as runtime
    from simstack.runner_runtime import core_build
    root = tmp_path / 'prepared'
    root.mkdir()
    _create_runtime_for_core_probe(root)
    # The prepared interpreter, not the caller's PYTHONPATH, must load Core.
    runtime.verify_runtime_core(root)
    copied = tmp_path / 'old-platform' / 'simstack' / 'runner_runtime'
    copied.mkdir(parents=True)
    (copied.parent / '__init__.py').write_text('')
    (copied / '__init__.py').write_text('')
    shutil.copy2(Path(core_build.__file__), copied / 'core_build.py')
    (copied.parent / 'static').mkdir()
    shutil.copy2(Path(core_build.__file__).parents[1] / 'static' / 'slurm_checkpoint.sh',
        copied.parent / 'static' / 'slurm_checkpoint.sh')
    (copied.parent / 'old_feature.py').write_text('OLD = True\n')
    python = runtime.runtime_python(root)
    site = json.loads(subprocess.check_output([str(python), '-I', '-c',
        'import json,site; print(json.dumps(site.getsitepackages()))'], text=True))[0]
    Path(site, 'simstack-platform.pth').write_text(str(copied.parent.parent) + '\n')
    with pytest.raises(RuntimeError, match='different Core build'):
        runtime.verify_runtime_core(root)


def test_prepared_core_snapshot_survives_in_place_runner_upgrade(tmp_path, monkeypatch):
    """Even a late import in an old environment must use its prepared Core."""
    import venv
    import simstack
    from types import SimpleNamespace
    from simstack.util import repository_runtime as runtime

    installed = tmp_path / "runner-install"
    package = installed / "simstack"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "late_science.py").write_text("VALUE = 'old'\n")
    metadata = installed / "simstack-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: simstack\nVersion: 1.0\n")
    monkeypatch.setattr(simstack, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(runtime.importlib.metadata, "distribution", lambda name: SimpleNamespace(_path=metadata))

    run = subprocess.run
    def create_environment(command, **kwargs):
        if command[1] == "venv":
            venv.EnvBuilder(with_pip=False).create(command[-1])
            return SimpleNamespace(returncode=0)
        return run(command, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", create_environment)
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.simstack]\nrunner-protocol = 3\n")
    first = runtime.prepare_environment(source, extras=[], runtime=tmp_path / "old-runtime")
    (package / "late_science.py").write_text("VALUE = 'new'\n")
    second = runtime.prepare_environment(source, extras=[], runtime=tmp_path / "new-runtime")
    command = ["-I", "-c", "from simstack.late_science import VALUE; print(VALUE)"]
    assert subprocess.check_output([str(first), *command], text=True).strip() == "old"
    assert subprocess.check_output([str(second), *command], text=True).strip() == "new"
    assert not (first.parent.parent / ".simstack-platform" / "simstack").is_symlink()


@pytest.mark.parametrize("manifest", [None, "[tool.simstack]\nrunner-protocol=2\n", '[tool.simstack]\nrunner-protocol="3"\n'])
def test_incompatible_workflow_manifest_is_rejected_before_environment_creation(tmp_path, monkeypatch, manifest):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / "source"
    source.mkdir()
    if manifest is not None:
        (source / "pyproject.toml").write_text(manifest)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("uv ran before compatibility gate"))
    with pytest.raises(ValueError, match="protocol|manifest|pyproject"):
        runtime.prepare_environment(source, extras=[])
    assert not (source / ".venv").exists()


def test_inventory_does_not_advertise_an_unsupported_manifest(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    root = tmp_path / "cache" / "prepared"
    root.parent.mkdir()
    init(root)
    (root / "pyproject.toml").write_text("[tool.simstack]\nrunner-protocol=2\n")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "Unsupported pinned workflow")
    descriptor = _descriptor(root, "a" * 64)
    descriptor["core_build"] = runtime.core_build_identity()
    (root / ".simstack-repository.json").write_text(json.dumps(descriptor))
    executable = runtime.runtime_python(root)
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    monkeypatch.setattr(runtime, "verify_runtime_core", lambda *args: pytest.fail("Unsupported interpreter was launched"))
    assert runtime.prepared_repositories(cache=root.parent) == []


def test_invalid_current_binding_is_not_ready_and_only_explicit_preparation_repairs_it(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / 'source'
    init(source)
    (source / 'value.py').write_text('VALUE=7\n')
    git(source, 'add', '.')
    git(source, 'commit', '--quiet', '-m', 'Scientific source')
    descriptor = _descriptor(source, 'f' * 64)
    monkeypatch.setattr(runtime, 'validate_clone_url', lambda value: value)
    monkeypatch.setattr(runtime, 'git_environment', lambda **kwargs: {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'})
    monkeypatch.setattr(runtime, 'prepare_environment', _create_runtime_for_core_probe)
    cache = tmp_path / 'cache'
    root = runtime._cache_revision(None, descriptor, cache=cache)
    current = json.loads((root / '.simstack-repository.json').read_text())
    assert current['core_build'] == runtime.core_build_identity()
    python = runtime.runtime_python(root)
    site = json.loads(subprocess.check_output([str(python), '-I', '-c',
        'import json,site; print(json.dumps(site.getsitepackages()))'], text=True))[0]
    from pathlib import Path
    binding = Path(site, 'simstack-platform.pth')
    binding.write_text(str(tmp_path / 'deleted-runner-installation') + '\n')
    assert runtime.prepared_repositories(cache=cache) == []
    stale = binding.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        runtime._cache_revision(None, descriptor, cache=cache)
    assert binding.read_bytes() == stale
    repaired = runtime._cache_revision(None, descriptor, cache=cache, repair_runtime=True)
    assert repaired == root
    runtime.verify_runtime_core(root)
    assert runtime.prepared_repositories(cache=cache) == [{'repository_revision': 'f' * 64, 'extras': []}]


def test_failed_explicit_runtime_repair_restores_previous_environment(tmp_path, monkeypatch):
    from simstack.util import repository_runtime as runtime
    source = tmp_path / 'source'
    init(source)
    (source / 'value.py').write_text('VALUE=1\n')
    git(source, 'add', '.')
    git(source, 'commit', '--quiet', '-m', 'Source')
    descriptor = _descriptor(source, '8' * 64)
    monkeypatch.setattr(runtime, 'validate_clone_url', lambda value: value)
    monkeypatch.setattr(runtime, 'git_environment', lambda **kwargs: {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'})
    monkeypatch.setattr(runtime, 'prepare_environment', lambda root, **kwargs: (root / '.venv').mkdir())
    monkeypatch.setattr(runtime, 'verify_runtime_core', lambda root: None)
    cache = tmp_path / 'cache'
    root = runtime._cache_revision(None, descriptor, cache=cache)
    previous = root / '.venv' / 'scientific-dependency'
    previous.write_text('original locked dependency')
    def stale(root):
        raise RuntimeError('Core binding is stale')
    monkeypatch.setattr(runtime, 'verify_runtime_core', stale)
    def fail_preparation(root, **kwargs):
        (root / '.venv').mkdir()
        (root / '.venv' / 'incomplete').touch()
        raise RuntimeError('uv unavailable')
    monkeypatch.setattr(runtime, 'prepare_environment', fail_preparation)
    with pytest.raises(RuntimeError, match='uv unavailable'):
        runtime._cache_revision(None, descriptor, cache=cache, repair_runtime=True)
    assert previous.read_text() == 'original locked dependency'
    assert not (root / '.venv' / 'incomplete').exists()
    assert not list(cache.glob('runtime-*'))
