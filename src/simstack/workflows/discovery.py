"""Persistence-free discovery of declared workflow modules and their contracts.

Importing Python is execution. Call this module only in the operator-sandboxed,
revision-specific runtime, never in a supervisor or the HTTP server process.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import inspect
import json
import re
import sys
import textwrap
import tomllib
from pathlib import Path
from types import FunctionType, ModuleType, UnionType
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from packaging.utils import canonicalize_name
from pydantic import BaseModel

from simstack.core.node_code_version import node_code_version
from simstack.core.simstack_result import SimstackResult
from simstack.models.document import REFERENCE_SCHEMA_KEY, Model
from simstack.models.document.reference_values import transform_reference_values
from simstack.models.parameters import Parameters
from simstack.util.docstring_parser import DocstringParser
from simstack.util.generate_ui_schema import resolve_ui_schema, ui_schema_identity

from .limits import DEFAULT_INSTALLATION_LIMITS
from .manifest import NodeOverride, WorkflowManifest, canonical_path, load_manifest


def object_mapping(value: Any) -> str:
    mapping = f"{value.__module__}.{value.__qualname__}"
    canonical_path(mapping)
    if "<locals>" in mapping or "." in value.__qualname__:
        raise ValueError("Workflow exports must be module-level classes/functions")
    return mapping


def _belongs_to_source(value: Any, root: Path) -> bool:
    try:
        origin = inspect.getfile(inspect.unwrap(value))
    except (TypeError, OSError):
        return False
    return Path(origin).resolve().is_relative_to(root)


def exported_function(modules: list[ModuleType], mapping: str) -> Any:
    """Resolve a canonical function, including explicit package re-exports."""
    matches = {
        value
        for module in modules
        for value in vars(module).values()
        if type(value) is FunctionType and object_mapping(value) == mapping
    }
    if len(matches) != 1:
        raise ValueError("Function is not a unique canonical exported function")
    return matches.pop()


def _source_snapshot_root(import_root: Path) -> Path:
    # source_root can be src/ or another contained package directory.
    for candidate in (import_root, *import_root.parents):
        if (candidate / "simstack-workflow.toml").is_file():
            return candidate
    return import_root


def _dependency_exports(import_root: Path) -> list[dict]:
    from .snapshot import read_snapshot

    root = _source_snapshot_root(import_root)
    metadata = read_snapshot(root)
    if metadata is None:
        return []
    exports = []
    for source in metadata["sources"]:
        project = tomllib.loads((root / source["path"] / "pyproject.toml").read_text())
        names = list(project.get("project", {}).get("entry-points", {}).get("simstack.modules", {}).values())
        try:
            distribution = importlib.metadata.distribution(source["name"])
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"Locked source package is not installed: {source['name']}") from exc
        from packaging.version import Version
        if (canonicalize_name(distribution.metadata["Name"]) != source["name"]
                or Version(distribution.version) != Version(source["version"])):
            raise ValueError("Installed source package does not match the snapshot version")
        if distribution.files is None:
            raise ValueError("Locked source package has no immutable installed file inventory")
        files = frozenset(Path(distribution.locate_file(item)).resolve() for item in distribution.files)
        modules = []
        for name in names:
            if not isinstance(name, str) or ":" in name or not all(part.isidentifier() for part in name.split(".")):
                raise ValueError("simstack.modules entry points must name canonical modules")
            modules.append(name)
        exports.append({"name": source["name"], "version": source["version"],
                        "modules": modules, "files": files})
    return exports


def _dependency_owner(value, dependencies):
    try:
        origin = Path(inspect.getfile(inspect.unwrap(value))).resolve()
    except (TypeError, OSError):
        return None
    owners = [dependency for dependency in dependencies if origin in dependency["files"]]
    if len(owners) > 1:
        raise ValueError("Installed packages disagree about ownership of workflow code")
    return owners[0] if owners else None


def effective_node_version(function, source_root, *, manifest=None, dependencies=None):
    """Use one version contract for catalogue discovery and task execution.

    A decorated version is explicit scientific API metadata. Otherwise bundled
    packages retain their own locked distribution versions. Only uv project's
    root nodes default to the manifest version; legacy archives keep None.
    """
    declared = getattr(function, "_node_version", None)
    if declared is not None:
        return declared
    root = Path(source_root).resolve(strict=True)
    manifest = manifest if manifest is not None else load_manifest(root)
    import_root = (root / manifest.workflow.source_root).resolve(strict=True)
    dependencies = dependencies if dependencies is not None else _dependency_exports(import_root)
    owner = _dependency_owner(function, dependencies)
    if owner is not None:
        return owner["version"]
    if (root / "uv.lock").is_file() and _belongs_to_source(function, import_root):
        return manifest.workflow.version
    return None


def load_modules(source_root: str | Path, names: list[str]) -> list[ModuleType]:
    """Load root exports and entry points of exact bundled distributions only."""
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workflow import root must be a directory")
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    from simstack.runner_runtime.environment import assert_database_runtime_unavailable

    assert_database_runtime_unavailable()
    modules = []
    requested = [(name, None) for name in names]
    for dependency in _dependency_exports(root):
        requested.extend((name, dependency) for name in dependency["modules"])
    for name, dependency in requested:
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        path = Path(origin).resolve() if origin is not None else None
        allowed = path is not None and (
            path.is_relative_to(root) if dependency is None else path in dependency["files"]
        )
        if not allowed:
            raise ValueError(f"Declared module {name!r} does not belong to this source snapshot" if dependency is None
                             else f"Declared dependency module {name!r} does not belong to its immutable source package")
        if module not in modules:
            modules.append(module)
    assert_database_runtime_unavailable()
    return modules


def _model_types(annotation: Any):
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
    else:
        for argument in get_args(annotation):
            yield from _model_types(argument)


def model_closure(seeds: list[type[Model]]) -> dict[str, type[Model]]:
    """Find Reference dependencies, including those inside embedded fields."""
    models: dict[str, type[Model]] = {}
    seen = set()
    pending = list(seeds)
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        if issubclass(model, Model):
            mapping = object_mapping(model)
            if mapping in models and models[mapping] is not model:
                raise ValueError(f"Conflicting class identities for {mapping}")
            models[mapping] = model
        for field_name, field in model.model_fields.items():
            if (field.json_schema_extra or {}).get(REFERENCE_SCHEMA_KEY):
                transform_reference_values(
                    None,
                    field.annotation,
                    lambda item, cls, path: item,
                    path=f"{model.__name__}.{field_name}",
                )
            pending.extend(_model_types(field.annotation))
    return models


def _concrete_model(annotation: Any, *, purpose: str) -> type[Model]:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, UnionType):
        choices = [item for item in get_args(annotation) if item is not type(None)]
        if len(choices) != 1:
            raise ValueError(f"{purpose} has an ambiguous model union")
        annotation = choices[0]
    if not inspect.isclass(annotation) or not issubclass(annotation, Model):
        raise ValueError(f"{purpose} must declare a document Model, not {annotation!r}")
    return annotation


def _lookup(expression: ast.expr, namespace: dict[str, Any]) -> Any:
    if isinstance(expression, ast.Name):
        return namespace.get(expression.id)
    if isinstance(expression, ast.Attribute):
        parent = _lookup(expression.value, namespace)
        if isinstance(parent, ModuleType):
            return vars(parent).get(expression.attr)
    return None


def _children(function: Any, source_root: Path, warnings: list[str], *, dependency_files=frozenset()) -> set[str]:
    """Resolve actual decorated callables, not short-name catalogue guesses."""
    found, visited = set(), set()

    def visit(candidate):
        candidate = inspect.unwrap(candidate)
        if candidate in visited:
            return
        visited.add(candidate)
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(candidate)))
        except (OSError, TypeError, SyntaxError) as exc:
            raise ValueError(
                f"Cannot inspect calls of {object_mapping(candidate)}; declare called_nodes explicitly"
            ) from exc
        # Local names are not module globals; never mistake a local variable for
        # a same-name imported node. Dynamic dispatch needs an explicit override.
        local_names = set(candidate.__code__.co_varnames)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id in local_names:
                warnings.append(
                    f"{object_mapping(candidate)}: local callable {call.func.id!r} cannot be identified statically; optional called_nodes declares dynamic children"
                )
                continue
            if not isinstance(call.func, (ast.Name, ast.Attribute)):
                warnings.append(
                    f"{object_mapping(candidate)}: dynamic callable cannot be identified statically; optional called_nodes declares dynamic children"
                )
                continue
            target = _lookup(call.func, candidate.__globals__)
            if inspect.isfunction(target):
                if getattr(target, "_is_node", False):
                    found.add(object_mapping(target))
                else:
                    source_file = inspect.getsourcefile(target)
                    if source_file and (
                        Path(source_file).resolve().is_relative_to(source_root)
                        or Path(source_file).resolve() in dependency_files
                    ):
                        visit(target)

    visit(function)
    found.discard(object_mapping(function))
    return found


def _result_class(
    mapping: str, module: ModuleType, available: dict[str, type[Model]]
) -> type[Model]:
    # Never eval docstrings or import undeclared arbitrary paths. Existing model
    # class objects in declared module globals provide the only name resolution.
    if mapping in available:
        return available[mapping]
    value = vars(module).get(mapping)
    if issubclass(type(value), type) and issubclass(value, Model):
        return value
    for value in vars(module).values():
        if (
            issubclass(type(value), type)
            and issubclass(value, Model)
            and object_mapping(value) == mapping
        ):
            return value
    raise ValueError(
        f"Result model {mapping!r} is not an imported canonical document model"
    )


def _outputs(function, module, hints, override: NodeOverride, available):
    return_type = hints.get("return", inspect.Signature.empty)
    declarations = override.results
    parser = DocstringParser(inspect.getdoc(function))
    if return_type is SimstackResult and declarations is None:
        documented = parser.simstack_results()
        if not documented:
            raise ValueError(
                f"{object_mapping(function)} returns SimstackResult; declare its named results"
            )
        declarations = []
        for name, value in documented.items():
            from .manifest import ResultDeclaration

            declarations.append(
                ResultDeclaration(
                    name=name,
                    mapping=value["type"].split(", optional")[0],
                    description=value["description"],
                )
            )
    if declarations is None:
        if return_type in (inspect.Signature.empty, None, type(None)):
            raise ValueError(
                f"{object_mapping(function)} must declare its return contract"
            )
        result_class = _concrete_model(return_type, purpose="Node return")
        return [
            {
                "name": "result",
                "mapping": object_mapping(result_class),
                "description": (parser.returns() or {}).get("description"),
                "version": None,
            }
        ], [result_class]
    if not declarations or len({item.name for item in declarations}) != len(
        declarations
    ):
        raise ValueError("Explicit results must be a non-empty list with unique names")
    if return_type not in (SimstackResult, bool):
        expected = _concrete_model(return_type, purpose="Node return")
        if (
            len(declarations) != 1
            or object_mapping(expected) != declarations[0].mapping
        ):
            raise ValueError("Explicit results do not match the declared return Model")
    models = [_result_class(item.mapping, module, available) for item in declarations]
    if return_type is bool and len(models) != 1:
        raise ValueError("Boolean results require exactly one explicit document model")
    outputs = [
        {
            "name": item.name,
            "mapping": object_mapping(model),
            "description": item.description,
            "version": None,
        }
        for item, model in zip(declarations, models)
    ]
    return outputs, models


def discover_workflow(
    source_root: str | Path, *, workflow_revision: str | None = None,
    max_manifest_bytes: int = DEFAULT_INSTALLATION_LIMITS.max_manifest_bytes,
) -> dict[str, Any]:
    root = Path(source_root).resolve(strict=True)
    manifest: WorkflowManifest = load_manifest(root, max_bytes=max_manifest_bytes)
    import_root = root / manifest.workflow.source_root
    import simstack
    from simstack.core.artifacts import ARTIFACT_TYPES

    core_root = Path(simstack.__file__).resolve().parent
    # Capture actual trusted class identities BEFORE executing package imports.
    # Uploaded classes can spoof __module__; a canonical name is not provenance.
    trusted_core, pending = set(), list(Model.__subclasses__())
    while pending:
        candidate = pending.pop()
        pending.extend(candidate.__subclasses__())
        if _belongs_to_source(candidate, core_root):
            trusted_core.add(candidate)
    modules = load_modules(import_root, manifest.workflow.modules)
    dependencies = _dependency_exports(import_root.resolve())
    dependency_files = frozenset(path for dependency in dependencies for path in dependency["files"])
    functions, available, seeds = {}, {}, []
    for module in modules:
        for value in vars(module).values():
            if (
                # isinstance/inspect can read proxy.__class__. Discovery must
                # not initialize imported runtime context objects to inspect it.
                issubclass(type(value), type)
                and issubclass(value, Model)
                and value is not Model
            ):
                available[object_mapping(value)] = value
                if _belongs_to_source(value, import_root.resolve()) or _dependency_owner(value, dependencies):
                    seeds.append(value)
            elif (
                type(value) is FunctionType
                and getattr(value, "_is_node", False)
                and (_belongs_to_source(value, import_root.resolve()) or _dependency_owner(value, dependencies))
            ):
                functions[object_mapping(value)] = (module, value)
    if not functions:
        raise ValueError("Declared workflow modules contain no decorated nodes")
    unknown_overrides = set(manifest.nodes) - set(functions)
    if unknown_overrides:
        raise ValueError(
            f"Overrides name undeclared nodes: {sorted(unknown_overrides)}"
        )
    nodes = []
    warnings = []
    for mapping, (module, function) in sorted(functions.items()):
        hints = get_type_hints(function, include_extras=True)
        override = manifest.nodes.get(mapping, NodeOverride())
        inputs = []
        parser = DocstringParser(inspect.getdoc(function))
        for parameter in inspect.signature(function).parameters.values():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            model = _concrete_model(
                hints.get(parameter.name), purpose=f"Input {mapping}.{parameter.name}"
            )
            seeds.append(model)
            inputs.append(
                {
                    "name": parameter.name,
                    "mapping": object_mapping(model),
                    "description": (
                        (parser.params() or {}).get(parameter.name) or {}
                    ).get("description"),
                    "version": None,
                }
            )
        outputs, output_models = _outputs(function, module, hints, override, available)
        seeds.extend(output_models)
        try:
            children = (
                _children(function, import_root.resolve(), warnings, dependency_files=dependency_files)
                if override.called_nodes is None
                else set(override.called_nodes)
            )
        except ValueError as exc:
            children = set()
            warnings.append(str(exc))
        unknown = children - set(functions)
        if unknown:
            warnings.append(
                f"{mapping} refers to child nodes outside declared modules: {sorted(unknown)}"
            )
        # Validate captured/default code identity before installing an executable
        # contract. The immutable source digest additionally versions helpers.
        try:
            function_hash = node_code_version(
                getattr(function, "_inner", function),
                workflow_revision=workflow_revision,
            )
        except (TypeError, ValueError) as exc:
            function_hash = None
            warnings.append(f"{mapping}: code identity is deferred to execution: {exc}")
        nodes.append(
            {
                "name": mapping,
                "function_mapping": mapping,
                "function_hash": function_hash,
                "version": effective_node_version(function, root, manifest=manifest, dependencies=dependencies),
                "input_mappings": inputs,
                "result_mappings": outputs,
                "called_nodes": sorted(children),
                "description": parser.description(),
                "favorite": False,
                "default_parameters": getattr(
                    function, "_node_parameters", Parameters()
                ).model_dump(mode="json"),
            }
        )
    models = []
    for mapping, model in sorted(model_closure(seeds).items()):
        native_artifact = next(
            (base for base in model.__mro__ if base in ARTIFACT_TYPES), None
        )
        schema = model.model_json_schema()
        presentation_version = None
        try:
            ui_schema = resolve_ui_schema(model)
            presentation_version = ui_schema_identity()
        except Exception as exc:
            ui_schema = None
            warnings.append(
                f"{mapping}: optional UI schema is unavailable: {type(exc).__name__}: {exc}"
            )
        origin = inspect.getfile(model)
        version = (
            manifest.workflow.version
            if Path(origin).resolve().is_relative_to(import_root.resolve())
            else (_dependency_owner(model, dependencies) or {}).get("version")
        )
        models.append(
            {
                "name": mapping,
                "mapping": mapping,
                "collection_name": native_artifact.__collection__
                if native_artifact is not None
                else model.__collection__,
                "json_schema": json.dumps(schema, sort_keys=True),
                "ui_schema": json.dumps(ui_schema, sort_keys=True)
                if ui_schema is not None
                else None,
                "ui_schema_version": presentation_version,
                "version": version,
                "source_kind": "core"
                if model in trusted_core
                else "package"
                if Path(origin).resolve().is_relative_to(import_root.resolve())
                else "dependency",
                **({"artifact_type": object_mapping(native_artifact)} if native_artifact is not None else {}),
            }
        )
    artifacts = []
    for declaration in manifest.artifacts:
        function = exported_function(modules, declaration.function_mapping)
        if not _belongs_to_source(function, import_root.resolve()):
            raise ValueError(
                "Artifact collector does not belong to this source snapshot"
            )
        if set(declaration.nodes) - set(functions):
            raise ValueError("Artifact collector refers to undeclared workflow nodes")
        artifacts.append(
            {
                "name": declaration.function_mapping,
                "function_mapping": declaration.function_mapping,
                "nodes": declaration.nodes,
                "regex_pattern": "^(?:"
                + "|".join(re.escape(value) for value in declaration.nodes)
                + ")$",
            }
        )
    # Revision dependencies can overlay operator base distributions. Preserve
    # the FIRST active distribution, rather than freezing conflicting versions
    # of the same package from every inherited site-packages directory.
    distributions = {}
    for dist in importlib.metadata.distributions():
        if Path(dist.locate_file("")).resolve().is_relative_to(root):
            continue  # Source archives cannot manufacture runtime lock metadata.
        name = canonicalize_name(dist.metadata["Name"])
        if name not in {"simstack", "pip", "setuptools", "wheel"}:
            distributions.setdefault(name, dist.version)
    resolved = sorted(f"{name}=={version}" for name, version in distributions.items())
    from .uv_project import _selected, load_uv_project
    project = load_uv_project(root)
    project_metadata = {}
    if project is not None:
        from .snapshot import read_snapshot
        lock = tomllib.loads((root / "uv.lock").read_text())
        snapshot = read_snapshot(root)
        source_versions = {entry["name"]: entry["version"] for entry in snapshot["sources"]} if snapshot else {}
        resolved = set()
        for package in lock.get("package", []):
            name = canonicalize_name(package["name"])
            version = package.get("version") or source_versions.get(name)
            if (_selected(package) and name not in {project.name, "simstack"}
                    and version is not None and distributions.get(name) == version):
                resolved.add(f"{name}=={version}")
        resolved = sorted(resolved)
        project_metadata = {"dependency_mode": "uv", "project_identity": project.identity,
                            "source_dependencies": list(snapshot["sources"]) if snapshot is not None else []}
    return {
        "package_id": manifest.workflow.id,
        "version": manifest.workflow.version,
        "runtime_profile": manifest.workflow.runtime_profile,
        "dependencies": list(project.dependencies) if project is not None else manifest.workflow.dependencies,
        "resolved_dependencies": resolved,
        **project_metadata,
        "modules": manifest.workflow.modules,
        "source_root": manifest.workflow.source_root,
        "models": models,
        "nodes": nodes,
        "artifacts": artifacts,
        "warnings": warnings,
    }
