"""Resource-local package preparation; all control-plane operations use HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from simstack.runner_runtime.environment import sanitized_trusted_helper_environment
from simstack.runner_runtime.execution_adapter import WorkflowInstallationAdapter
from simstack.workflows.archive import (
    archive_digest,
    expanded_entry_count,
    extract_archive,
    validate_archive,
)
from simstack.workflows.dependencies import (
    WorkflowDependencyError,
    bootstrap_digests,
    interpreter_digest,
    validate_dependency_environment,
)
from simstack.workflows.limits import (
    DEFAULT_INSTALLATION_LIMITS,
    WorkflowInstallationLimits,
)
from simstack.workflows.manifest import (
    WorkflowManifest,
    executable_paths,
    load_manifest,
)
from simstack.workflows.processes import run_process
from simstack.workflows.uv_project import (
    CORE_INFO_SCRIPT,
    load_uv_project,
    uv_preparation_commands,
    validate_platform_core,
)


def installation_root(workdir: Path, installation_id: str, revision: str) -> Path:
    if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("Workflow revision must be canonical SHA256")
    identity = hashlib.sha256(installation_id.encode()).hexdigest()
    root = workdir.resolve() / "workflow-packages" / identity / revision
    root.resolve().relative_to(workdir.resolve())
    return root


def _run(
    command: list[str], *, input_data: dict | None = None, timeout: int = 600,
    limits: WorkflowInstallationLimits | None = None,
    cwd: Path | None = None, storage_root: Path | None = None,
) -> subprocess.CompletedProcess:
    environment = sanitized_trusted_helper_environment()
    # Match the UTF-8 .pth decoder in the filesystem-only dependency guard.
    environment.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    actual_limits = limits or DEFAULT_INSTALLATION_LIMITS
    native_policy = actual_limits.native_thread_environment()
    environment.update(native_policy)
    environment["SIMSTACK_WORKFLOW_NATIVE_THREAD_POLICY"] = json.dumps(native_policy)
    environment["SIMSTACK_WORKFLOW_INSTALL_LIMITS"] = json.dumps(actual_limits.to_dict())
    if cwd is not None:
        environment["TMPDIR"] = str(cwd.resolve())
    result = run_process(
        command,
        input_data=None if input_data is None else json.dumps(input_data, allow_nan=False).encode(),
        cwd=cwd,
        limits=limits,
        storage_root=storage_root,
        timeout=timeout,
        env=environment,
    )
    completed = subprocess.CompletedProcess(
        command, result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )
    completed.check_returncode()
    return completed


@contextmanager
def _installation_lock(root: Path, *, name: str = ".installation.lock", blocking: bool = True):
    """Serialize preparation, or shared-storage reservations across installations."""
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError(
            "Workflow installation requires supported POSIX file locking"
        ) from exc
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            # A separate open avoids macOS openat(O_CREAT | O_NOFOLLOW)
            # spuriously returning ENOENT when another preparer creates it.
            descriptor = os.open(name, flags, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            from simstack.workflows.processes import WorkflowProcessLimitError
            raise WorkflowProcessLimitError("Workflow storage staging is already in progress") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_guard(base_python: str, guard: tuple[str, ...]) -> None:
    if (
        not isinstance(base_python, str)
        or not base_python.strip()
        or not guard
        or any(not isinstance(item, str) or not item.strip() for item in guard)
    ):
        raise ValueError(
            "Workflow installation requires installer_python and an operator-owned process boundary argv"
        )


def _validate_descriptor(package) -> WorkflowManifest:
    manifest = WorkflowManifest.model_validate(package.manifest)
    if (
        manifest.workflow.id != package.package_id
        or manifest.workflow.runtime_profile != package.runtime_profile
    ):
        raise ValueError(
            "Workflow package identity or runtime profile disagrees with its manifest"
        )
    return manifest


def _verify_source_snapshot(data: bytes | Path, source: Path, *, limits=DEFAULT_INSTALLATION_LIMITS) -> None:
    """Check stored bytes, never import or execute workflow code to validate the cache."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Installed workflow source directory is missing or invalid")
    members = validate_archive(data, **limits.archive_options())
    allowed = set()
    for member in members:
        relative = Path(member.name)
        allowed.add(relative.as_posix().rstrip("/"))
        allowed.update(
            parent.as_posix() for parent in relative.parents if parent != Path(".")
        )
    for current in source.rglob("*"):
        if (
            current.is_symlink()
            or current.relative_to(source).as_posix() not in allowed
        ):
            raise ValueError(
                "Installed workflow source contains an unregistered file or symbolic link"
            )
    try:
        declared_executables = set(executable_paths(source, max_bytes=limits.max_manifest_bytes))
    except FileNotFoundError as exc:
        raise ValueError("Installed workflow source manifest is missing") from exc
    with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data) as archive:
        for member in members:
            target = source / member.name
            target.resolve().relative_to(source.resolve())
            current = target
            while current != source:
                if current.is_symlink():
                    raise ValueError(
                        "Installed workflow source contains a replaced symbolic link"
                    )
                current = current.parent
            if member.directory:
                if not target.is_dir():
                    raise ValueError(
                        "Installed workflow source directory does not match its archive"
                    )
                continue
            if not target.is_file() or target.stat().st_size != member.size:
                raise ValueError(
                    "Installed workflow source file does not match its archive"
                )
            if bool(target.stat().st_mode & 0o111) != (member.executable or member.name in declared_executables):
                raise ValueError("Installed workflow executable permissions do not match its archive or manifest")
            archived = hashlib.sha256()
            extracted = hashlib.sha256()
            with archive.open(member.name) as expected, target.open("rb") as actual:
                while block := expected.read(1024 * 1024):
                    archived.update(block)
                while block := actual.read(1024 * 1024):
                    extracted.update(block)
            if archived.digest() != extracted.digest():
                raise ValueError(
                    "Installed workflow source checksum does not match its archive"
                )


def _ready_runtime(root: Path, *, revision: str, manifest: dict | None = None, limits: WorkflowInstallationLimits | None = None) -> dict:
    marker = root / "ready.json"
    if not marker.is_file() or marker.is_symlink():
        raise ValueError(
            "The task's exact workflow environment is not prepared on this resource"
        )
    ready = json.loads(marker.read_text())
    if (
        not isinstance(ready, dict)
        or ready.get("revision") != revision
        or not isinstance(ready.get("manifest"), dict)
        or not isinstance(ready.get("base_python"), str)
    ):
        raise ValueError(
            "Cached workflow runtime readiness record has an invalid identity"
        )
    if manifest is not None and ready["manifest"] != manifest:
        raise ValueError(
            "Cached workflow runtime manifest does not match its installation"
        )
    environment = root / "environment"
    environment.resolve().relative_to(root.resolve())
    python = environment / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Cached workflow runtime executable is missing or unusable")
    sites = ready.get("core_sites")
    if not isinstance(sites, list) or not sites or not all(isinstance(path, str) and Path(path).is_absolute() for path in sites):
        raise WorkflowDependencyError("Cached workflow environment has no verified operator Core paths; prepare it again")
    python_sha256 = ready.get("python_sha256")
    bootstrap = ready.get("bootstrap")
    if not isinstance(python_sha256, str) or not isinstance(bootstrap, dict):
        raise WorkflowDependencyError("Cached workflow environment has no trusted Python declaration; prepare it again")
    validate_dependency_environment(environment, trusted_sites=sites, operator_pth="simstack-core-runtime.pth", expected_python_sha256=python_sha256, expected_bootstrap=bootstrap)
    limits = limits or WorkflowInstallationLimits.from_mapping(ready.get("installation_limits"))
    data = root / "archive.zip"
    validate_archive(data, **limits.archive_options())
    if archive_digest(data) != revision:
        raise ValueError("Installed workflow archive checksum mismatch")
    _verify_source_snapshot(data, root / "source", limits=limits)
    project = load_uv_project(root / "source")
    if project is not None and ready.get("project_identity") != project.identity:
        raise WorkflowDependencyError("Cached workflow runtime does not match its uv project lock; prepare it again")
    return ready


def prepare_package(
    root: Path,
    *,
    revision: str,
    manifest: dict,
    base_python: str,
    guard: tuple[str, ...],
    limits: WorkflowInstallationLimits = DEFAULT_INSTALLATION_LIMITS,
) -> Path:
    """Build only a private environment; package imports occur behind its guard."""
    _validate_guard(base_python, guard)
    if root.is_symlink():
        raise ValueError("Workflow installation root must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _installation_lock(root):
        return _prepare_package_locked(
            root,
            revision=revision,
            manifest=manifest,
            base_python=base_python,
            guard=guard,
            limits=limits,
        )


def _prepare_package_locked(
    root: Path,
    *,
    revision: str,
    manifest: dict,
    base_python: str,
    guard: tuple[str, ...],
    limits: WorkflowInstallationLimits = DEFAULT_INSTALLATION_LIMITS,
) -> Path:
    archive_path = root / "archive.zip"
    data = archive_path
    validate_archive(data, **limits.archive_options())
    if archive_digest(data) != revision:
        raise ValueError("Cached workflow archive checksum mismatch")
    desired = WorkflowManifest.model_validate(manifest)
    source = root / "source"
    if source.exists():
        try:
            _verify_source_snapshot(data, source, limits=limits)
        except ValueError:
            # The source directory is an immutable installer-owned snapshot.
            # Recover old interrupted extraction only before runtime readiness;
            # a damaged ready cache remains an error rather than being trusted.
            if (root / "ready.json").exists() or source.is_symlink() or not source.is_dir():
                raise
            shutil.rmtree(source)
    if not source.exists():
        from simstack.workflows.processes import (
            WorkflowProcessLimitError,
            check_storage_budget,
        )
        storage_root = root.parents[1] if root.parent.parent.name == "workflow-packages" else root
        with _installation_lock(storage_root, name=".storage.lock", blocking=False):
            staging_source = root / ".source-pending"
            if staging_source.exists() or staging_source.is_symlink():
                if staging_source.is_symlink() or not staging_source.is_dir():
                    raise ValueError("Workflow source staging directory is invalid")
                shutil.rmtree(staging_source)
            members = validate_archive(data, **limits.archive_options())
            reserved = sum(member.size for member in members)
            reserved_files = expanded_entry_count(members) + 1  # source root
            if reserved >= limits.max_storage_bytes:
                raise WorkflowProcessLimitError("Workflow installation storage budget exceeded")
            if reserved_files >= limits.max_storage_files:
                raise WorkflowProcessLimitError("Workflow installation storage entry budget exceeded")
            check_storage_budget(storage_root, limits.max_storage_bytes - reserved, max_files=limits.max_storage_files - reserved_files)
            try:
                extract_archive(data, staging_source, **limits.archive_options())
                _verify_source_snapshot(data, staging_source, limits=limits)
                staging_source.rename(source)
            finally:
                if staging_source.exists():
                    shutil.rmtree(staging_source)
    _verify_source_snapshot(data, source, limits=limits)
    actual = load_manifest(source, max_bytes=limits.max_manifest_bytes)
    uv_project = load_uv_project(source)
    for field in ("id", "version", "source_root", "modules", "runtime_profile", "executables"):
        if getattr(actual.workflow, field) != getattr(desired.workflow, field):
            raise ValueError(f"Workflow source disagrees with its registered {field}")
    if actual.nodes != desired.nodes or actual.artifacts != desired.artifacts:
        raise ValueError(
            "Workflow source declarations disagree with its registered manifest"
        )
    marker = root / "ready.json"
    if marker.exists():
        try:
            ready = _ready_runtime(root, revision=revision, manifest=manifest, limits=limits)
        except WorkflowDependencyError:
            # Do not run or trust a rejected cache. An explicit installer retry
            # can replace its environment under the preparation lock; workers
            # still fail closed in _ready_runtime.
            marker.unlink()
            old = root / "environment"
            if old.is_symlink():
                target = old.resolve()
                old.unlink()
                if target.parent == root.resolve() and target.name.startswith(".environment-"):
                    shutil.rmtree(target)
            elif old.is_dir():
                shutil.rmtree(old)
        else:
            if any(ready.get(key) != value for key, value in {
                "revision": revision,
                "manifest": manifest,
                "base_python": base_python,
            }.items()):
                raise ValueError("Cached workflow runtime does not match its installation")
            return root / "environment" / "bin" / "python"
    staging = Path(tempfile.mkdtemp(prefix=".environment-", dir=root))
    environment = root / "environment"
    published_environment = False
    temporary_marker = None
    storage_root = root.parents[1] if root.parent.parent.name == "workflow-packages" else root
    process_options = {"limits": limits, "cwd": root, "storage_root": storage_root}
    try:
        _run([base_python, "-I", "-m", "venv", str(staging)], **process_options)
        python = staging / "bin" / "python"
        python_sha256 = interpreter_digest(python)
        bootstrap = bootstrap_digests(staging)
        if uv_project is not None:
            platform = json.loads(_run([base_python, "-I", "-c", CORE_INFO_SCRIPT], **process_options).stdout)
            validate_platform_core(uv_project, platform)
        elif desired.workflow.dependencies:
            _run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--only-binary=:all:",
                    "--no-cache-dir",
                    *desired.workflow.dependencies,
                ],
                **process_options,
            )
        base_paths = json.loads(
            _run(
                [
                    base_python,
                    "-I",
                    "-c",
                    "import json,sysconfig; print(json.dumps([sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]))",
                ],
                **process_options,
            ).stdout
        )
        version = json.loads(
            _run(
                [
                    base_python,
                    "-I",
                    "-c",
                    "import json,sys; print(json.dumps(list(sys.version_info[:2])))",
                ],
                **process_options,
            ).stdout
        )
        site_path = (
            staging / "lib" / f"python{version[0]}.{version[1]}" / "site-packages"
        )
        if not site_path.is_dir():
            raise ValueError(
                "Prepared environment has no supported Python site-packages directory"
            )
        # This .pth contains only operator-owned Core paths, not executable lines.
        (site_path / "simstack-core-runtime.pth").write_text(
            "\n".join(dict.fromkeys(base_paths)) + "\n"
        )
        validate_dependency_environment(staging, trusted_sites=base_paths, operator_pth="simstack-core-runtime.pth", expected_python_sha256=python_sha256, expected_bootstrap=bootstrap)
        if uv_project is not None:
            build_source = root / ".uv-build-source"
            if build_source.exists() or build_source.is_symlink():
                if build_source.is_symlink() or not build_source.is_dir():
                    raise ValueError("uv workflow build directory is invalid")
                shutil.rmtree(build_source)
            shutil.copytree(source, build_source)
            try:
                build_project = load_uv_project(build_source)
                commands = uv_preparation_commands(build_project, staging, base_python, cache_dir=root / ".uv-cache")
                _run(commands["prefetch"], **process_options)
                validate_dependency_environment(staging, trusted_sites=base_paths, operator_pth="simstack-core-runtime.pth", expected_python_sha256=python_sha256, expected_bootstrap=bootstrap)
                if commands["build_dependencies"] is not None:
                    _run(commands["build_dependencies"], **process_options)
                    validate_dependency_environment(staging, trusted_sites=base_paths, operator_pth="simstack-core-runtime.pth", expected_python_sha256=python_sha256, expected_bootstrap=bootstrap)
                # Source build backends execute only behind the operator's
                # installation boundary, with cached dependencies and no fetch.
                _run([*guard, *commands["sync"]], **process_options)
                validate_dependency_environment(staging, trusted_sites=base_paths, operator_pth="simstack-core-runtime.pth", expected_python_sha256=python_sha256, expected_bootstrap=bootstrap)
            finally:
                shutil.rmtree(build_source)
        request = {
            "operation": "discover",
            "source_root": str(source),
            "workflow_revision": revision,
            "max_manifest_bytes": limits.max_manifest_bytes,
        }
        probe = _run(
            [*guard, str(python), "-I", "-B", "-m", "simstack.workflows.runtime"],
            input_data=request,
            **process_options,
        )
        result = json.loads(probe.stdout)
        if result.get("ok") is not True:
            raise ValueError(
                f"Workflow runtime preparation failed: {result.get('error')}"
            )
        _verify_source_snapshot(data, source, limits=limits)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError(
                "Prepared workflow runtime executable is missing or unusable"
            )
        # venv scripts contain absolute staging paths: retain this owned directory.
        if environment.exists() or environment.is_symlink():
            raise ValueError("Workflow environment exists without a readiness record")
        environment.symlink_to(staging.name, target_is_directory=True)
        published_environment = True
        with tempfile.NamedTemporaryFile(
            mode="w", dir=root, prefix=".ready-", delete=False
        ) as stream:
            temporary_marker = Path(stream.name)
            json.dump(
                {
                    "revision": revision,
                    "manifest": manifest,
                    "base_python": base_python,
                    "core_sites": base_paths,
                    "python_sha256": python_sha256,
                    "bootstrap": bootstrap,
                    "installation_limits": limits.to_dict(),
                    **({"project_identity": uv_project.identity} if uv_project is not None else {}),
                },
                stream,
                sort_keys=True,
            )
        temporary_marker.replace(marker)
        temporary_marker = None
    except BaseException:
        if temporary_marker is not None:
            temporary_marker.unlink(missing_ok=True)
        if published_environment:
            environment.unlink()
        # Only the staging directory created by this attempt is removable.
        staging.resolve().relative_to(root.resolve())
        shutil.rmtree(staging)
        raise
    return environment / "bin" / "python"


async def install_package(
    client,
    descriptor: dict,
    *,
    workdir: Path,
    profile: WorkflowInstallationAdapter,
    claim_id: str,
    attempt: int,
    task_id: str | None = None,
) -> Path:
    from simstack.runner_protocol import RunnerWorkflowPackageV1

    package = RunnerWorkflowPackageV1.model_validate(descriptor)
    _validate_descriptor(package)
    if not isinstance(profile, WorkflowInstallationAdapter):
        raise ValueError(
            "Workflow installation requires a typed operator-owned runtime profile"
        )
    base_python = profile.base_python
    guard = profile.boundary.network_guard_command
    _validate_guard(base_python, guard)
    root = installation_root(workdir, package.installation_id, package.revision)
    if root.is_symlink():
        raise ValueError("Workflow installation root must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.resolve().relative_to(Path(workdir).resolve())
    archive = root / "archive.zip"
    from simstack.workflows.processes import (
        WorkflowProcessLimitError,
        check_storage_budget,
    )
    with _installation_lock(root.parent.parent, name=".storage.lock", blocking=False):
        reserved = 0 if archive.exists() else profile.limits.max_archive_bytes
        if reserved >= profile.limits.max_storage_bytes:
            raise WorkflowProcessLimitError("Workflow archive exceeds configured storage budget")
        await asyncio.to_thread(
            check_storage_budget, root.parent.parent,
            profile.limits.max_storage_bytes - reserved,
            max_files=profile.limits.max_storage_files - 1,
        )
        await client.download_workflow_archive(
            package.installation_id,
            archive,
            revision=package.revision,
            claim_id=claim_id,
            attempt=attempt,
            max_bytes=profile.limits.max_archive_bytes,
            **({"task_id": task_id} if task_id is not None else {}),
        )
    return await asyncio.to_thread(
        prepare_package,
        root,
        revision=package.revision,
        manifest=package.manifest,
        base_python=base_python,
        guard=tuple(guard),
        limits=profile.limits,
    )


async def ensure_claimed_package_runtime(
    client,
    registry,
    *,
    claim_id: str,
    attempt: int,
    workdir: Path,
    adapter,
) -> None:
    """Repair only approved, exact claim packages before a task worker starts."""
    from simstack.runner_protocol import RunnerWorkflowPackageV1

    fields = ("workflow_installation_id", "workflow_package", "workflow_revision", "runtime_profile")
    binding = tuple(getattr(registry, field, None) for field in fields)
    if all(value is None for value in binding):
        return
    if not all(isinstance(value, str) and value for value in binding):
        raise ValueError("Packaged workflow task has an incomplete pinned identity")
    bootstrap = await client.task_bootstrap(str(registry.id), claim_id=claim_id, attempt=attempt)
    descriptors = bootstrap.get("workflow_packages")
    if not isinstance(descriptors, list):
        raise ValueError("Claim bootstrap has no workflow package descriptors")
    packages = [RunnerWorkflowPackageV1.model_validate(value) for value in descriptors]
    identities = [(package.installation_id, package.package_id, package.revision, package.runtime_profile) for package in packages]
    if len(identities) != len(set(identities)) or identities.count(binding) != 1:
        raise ValueError("Claim bootstrap does not identify the task's exact workflow installation")
    preparations = []
    # Reject malformed declarations before any helper can install dependencies.
    for package in packages:
        _validate_descriptor(package)
        profile = adapter.workflow_installer(package.runtime_profile)
        if not isinstance(profile, WorkflowInstallationAdapter):
            raise ValueError("Workflow preparation requires an operator-owned runtime profile")
        _validate_guard(profile.base_python, profile.boundary.network_guard_command)
        root = installation_root(workdir, package.installation_id, package.revision)
        preparations.append((package, profile, root))
    for package, profile, root in preparations:
        if not (root / "archive.zip").exists():
            cold_preparation = asyncio.create_task(install_package(
                client, package.model_dump(mode="json"), workdir=workdir,
                profile=profile, claim_id=claim_id, attempt=attempt,
                task_id=str(registry.id),
            ))
            try:
                await asyncio.shield(cold_preparation)
            except asyncio.CancelledError:
                await asyncio.gather(cold_preparation, return_exceptions=True)
                raise
            continue
        preparation = asyncio.create_task(asyncio.to_thread(
            prepare_package, root, revision=package.revision, manifest=package.manifest,
            base_python=profile.base_python, guard=profile.boundary.network_guard_command,
            limits=profile.limits,
        ))
        try:
            await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # to_thread cancellation does not stop its thread. Join the bounded
            # trusted preparation before returning; no task worker can launch.
            await asyncio.gather(preparation, return_exceptions=True)
            raise


def packaged_worker_command(
    command: tuple[str, ...], registry, *, workdir: Path
) -> tuple[str, ...]:
    """Resolve explicit placeholders only for a pinned, prepared package."""
    revision = getattr(registry, "workflow_revision", None)
    if revision is None:
        if any(
            getattr(registry, field, None) is not None
            for field in (
                "workflow_package",
                "workflow_installation_id",
                "runtime_profile",
            )
        ):
            raise ValueError("Packaged workflow task is missing its pinned revision")
        return command
    installation_id = getattr(registry, "workflow_installation_id", None)
    if not isinstance(installation_id, str) or not installation_id:
        raise ValueError(
            "Packaged workflow task is missing its exact installation identity"
        )
    root = installation_root(workdir, installation_id, revision)
    ready = _ready_runtime(root, revision=revision)
    manifest = WorkflowManifest.model_validate(ready["manifest"])
    if manifest.workflow.id != getattr(
        registry, "workflow_package", None
    ) or manifest.workflow.runtime_profile != getattr(
        registry, "runtime_profile", None
    ):
        raise ValueError(
            "The task's workflow installation identity does not match its runtime"
        )
    replacements = {
        "{python}": str(root / "environment" / "bin" / "python"),
        "{source}": str(root / "source"),
        "{runtime}": str(root / "environment"),
    }
    rendered = tuple(replacements.get(item, item) for item in command)
    if any("{" in item or "}" in item for item in rendered):
        raise ValueError("Workflow worker command has unsupported placeholders")
    return rendered


def configure_package_imports(descriptors: list[dict], *, workdir: Path) -> None:
    """Claim bootstrap supplies exact source identity before user-code imports."""
    from simstack.runner_protocol import RunnerWorkflowPackageV1

    paths = []
    for descriptor in descriptors:
        package = RunnerWorkflowPackageV1.model_validate(descriptor)
        manifest = _validate_descriptor(package)
        root = installation_root(workdir, package.installation_id, package.revision)
        _ready_runtime(root, revision=package.revision, manifest=package.manifest)
        source = (root / "source" / manifest.workflow.source_root).resolve()
        source.relative_to((root / "source").resolve())
        if not source.is_dir():
            raise ValueError("Installed workflow import source directory is missing")
        paths.append(str(source))
    sys.path[:0] = paths
    # Source snapshots must remain byte-for-byte reproducible. Workflow outputs
    # belong in the task workdir, never in import caches next to package source.
    sys.dont_write_bytecode = True
