"""Check private dependency environments without importing uploaded packages."""

from email.parser import Parser
from pathlib import Path
import re
import hashlib
import io

from packaging.utils import canonicalize_name


PROTECTED_DISTRIBUTIONS = frozenset({"simstack", "odmantic", "motor", "pymongo", "bson"})
PROTECTED_MODULES = PROTECTED_DISTRIBUTIONS | {"sitecustomize", "usercustomize"}


class WorkflowDependencyError(ValueError):
    """A private environment violates the operator-owned import boundary."""


def _protected_module(path: Path) -> bool:
    return path.name.split(".", 1)[0] in PROTECTED_MODULES


def interpreter_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def bootstrap_digests(environment: Path) -> dict[str, str]:
    """Snapshot trusted venv startup files before installing any dependencies."""
    paths = [environment / "pyvenv.cfg"]
    paths.extend(path for path in (environment / "bin").iterdir() if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", path.name))
    return {path.relative_to(environment).as_posix(): interpreter_digest(path) for path in paths}


def _validate_bootstrap_layout(root: Path) -> None:
    # CPython can consult an executable/shared-library-adjacent ._pth before
    # ordinary site-packages. A wheel's scripts/data scheme can create one
    # without changing the executable or the original root pyvenv.cfg.
    for directory in (root, root / "bin", root / "lib"):
        if directory.is_symlink():
            raise WorkflowDependencyError("Workflow Python bootstrap directory was replaced")
        if not directory.is_dir():
            continue
        if any(path.name.endswith("._pth") for path in directory.iterdir()):
            raise WorkflowDependencyError("Workflow dependency cannot override Python startup paths")
    for name in ("pyvenv.cfg", "pybuilddir.txt"):
        if (root / "bin" / name).exists():
            raise WorkflowDependencyError("Workflow dependency cannot override Python startup configuration")


def validate_dependency_environment(
    environment: Path,
    *,
    trusted_sites: list[str] | tuple[str, ...],
    operator_pth: str,
    expected_python_sha256: str | None = None,
    expected_bootstrap: dict[str, str] | None = None,
) -> None:
    """Only Core's exact operator .pth may expose paths outside the private venv.

    Other dependency versions retain their normal precedence. Uploaded wheels
    cannot supply Core/DB namespaces or interpreter-startup code, including via
    a renamed distribution or an additional static .pth directory.
    """
    root = environment.resolve(strict=True)
    _validate_bootstrap_layout(root)
    if expected_bootstrap is not None:
        if "pyvenv.cfg" not in expected_bootstrap or "bin/python" not in expected_bootstrap:
            raise WorkflowDependencyError("Workflow environment has no trusted bootstrap declaration")
        for name, digest in expected_bootstrap.items():
            if name != "pyvenv.cfg" and not re.fullmatch(r"bin/python(?:\d+(?:\.\d+)*)?", name):
                raise WorkflowDependencyError("Workflow bootstrap declaration contains an invalid path")
            if interpreter_digest(root / name) != digest:
                raise WorkflowDependencyError("Workflow dependency replaced its trusted Python bootstrap")
    if expected_python_sha256 is not None:
        if len(expected_python_sha256) != 64 or interpreter_digest(root / "bin" / "python") != expected_python_sha256:
            raise WorkflowDependencyError("Workflow dependency replaced its trusted Python executable")
    sites = list(root.glob("lib/python*/site-packages"))
    if not sites:
        raise WorkflowDependencyError("Workflow environment has no private site-packages")
    expected = "\n".join(dict.fromkeys(trusted_sites)) + "\n"
    checked: set[Path] = set()
    pending = sites[:]
    found_operator_path = False
    while pending:
        site = pending.pop().resolve(strict=True)
        if not site.is_relative_to(root):
            raise WorkflowDependencyError("Workflow dependency path escapes its private environment")
        if site in checked:
            continue
        checked.add(site)
        for item in site.iterdir():
            if _protected_module(item):
                raise WorkflowDependencyError(f"Workflow dependency cannot provide protected module {item.name}")
            if item.name.endswith(".dist-info"):
                metadata = item / "METADATA"
                if metadata.is_file():
                    with metadata.open("rb") as stream:
                        headers = stream.read(64 * 1024).decode("utf-8", errors="strict")
                    name = Parser().parsestr(headers, headersonly=True).get("Name")
                    if not name:
                        raise WorkflowDependencyError("Workflow dependency metadata has no distribution name")
                    if canonicalize_name(name.strip()) in PROTECTED_DISTRIBUTIONS:
                        raise WorkflowDependencyError(f"Workflow dependency cannot install protected distribution {name}")
            if not item.name.endswith(".pth"):
                continue
            if item.is_symlink() or item.stat().st_size > 64 * 1024:
                raise WorkflowDependencyError("Workflow dependency startup paths are invalid")
            content = item.read_text(encoding="utf-8")
            if item.name == operator_pth and item.parent in sites:
                if content != expected:
                    raise WorkflowDependencyError("Workflow operator Core paths were replaced")
                found_operator_path = True
                continue
            # Match site.addpackage exactly: universal-newline file iteration,
            # then rstrip. splitlines() also splits Unicode path characters and
            # skipping trailing whitespace would validate a different path.
            for raw_line in io.StringIO(content, newline=None):
                line = raw_line.rstrip()
                if not line or line.startswith("#"):
                    continue
                if re.match(r"import[\t ]", line):
                    raise WorkflowDependencyError("Workflow dependencies cannot install executable startup hooks")
                path = (site / line).resolve()
                if not path.is_relative_to(root):
                    raise WorkflowDependencyError("Workflow dependency .pth path escapes its private environment")
                if path.exists():
                    if not path.is_dir():
                        raise WorkflowDependencyError("Workflow dependency .pth must refer to a directory")
                    pending.append(path)
    if not found_operator_path:
        raise WorkflowDependencyError("Workflow environment is missing its operator Core paths")
