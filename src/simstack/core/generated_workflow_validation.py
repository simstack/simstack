from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable


class GeneratedWorkflowValidationError(ValueError):
    def __init__(self, errors: Iterable[str]):
        self.errors = list(dict.fromkeys(errors))
        super().__init__(
            self.errors[0] if self.errors else "generated source is unsafe"
        )


_ALLOWED_IMPORTS = {
    "collections",
    "dataclasses",
    "decimal",
    "enum",
    "fractions",
    "functools",
    "itertools",
    "math",
    "statistics",
    "typing",
    "odmantic",
    "pydantic",
    "simstack.core.node",
    "simstack.core.simstack_result",
    "simstack.models",
}
_FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "hasattr",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}
_FORBIDDEN_METHODS = {
    "chmod",
    "chown",
    "connect",
    "delete",
    "execute",
    "executescript",
    "hardlink_to",
    "from_local_file",
    "make_info_files",
    "mkdir",
    "open",
    "read_bytes",
    "read_text",
    "remove",
    "rename",
    "replace",
    "request",
    "rmdir",
    "save",
    "subprocess",
    "symlink_to",
    "touch",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
}
_ALLOWED_DECORATORS = {"node", "simstack_model"}
_TRUSTED_DECORATOR_IMPORTS = {
    ("simstack.core.node", "node"): "node",
    ("simstack.models", "simstack_model"): "simstack_model",
    ("simstack.models.simstack_model", "simstack_model"): "simstack_model",
}


def _qualified_name(value: ast.expr) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        parent = _qualified_name(value.value)
        return f"{parent}.{value.attr}" if parent else value.attr
    return None


def _is_literal(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(item) for item in value.elts)
    if isinstance(value, ast.Dict):
        return all(
            key is not None and _is_literal(key) and _is_literal(item)
            for key, item in zip(value.keys, value.values, strict=True)
        )
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)):
        return _is_literal(value.operand)
    return False


def _bound_name_counts(tree: ast.Module) -> Counter[str]:
    """Count bindings that could shadow a trusted decorator import alias."""

    counts: Counter[str] = Counter()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                counts[alias.asname or alias.name.split(".", 1)[0]] += 1
        elif isinstance(item, ast.ImportFrom):
            for alias in item.names:
                counts[alias.asname or alias.name] += 1
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[item.name] += 1
        elif isinstance(item, ast.arg):
            counts[item.arg] += 1
        elif isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            counts[item.id] += 1
        elif isinstance(item, ast.ExceptHandler) and item.name:
            counts[item.name] += 1
        elif isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
            counts[item.name] += 1
    return counts


def generated_workflow_decorator_bindings(tree: ast.Module) -> dict[str, str]:
    """Return canonical decorators plus safe aliases from exact trusted imports.

    An alias is trusted only when its sole binding in the module is one explicit
    ``from ... import ... as ...`` statement. This prevents generated code from
    importing a trusted decorator and then shadowing that name before use.
    """

    bindings: dict[str, str] = {}
    bound_name_counts = _bound_name_counts(tree)
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or statement.level:
            continue
        for alias in statement.names:
            decorator_name = _TRUSTED_DECORATOR_IMPORTS.get(
                (statement.module or "", alias.name)
            )
            bound_name = alias.asname or alias.name
            if decorator_name is not None and bound_name_counts[bound_name] == 1:
                bindings[bound_name] = decorator_name
    return bindings


def _validate_import(node: ast.Import | ast.ImportFrom, errors: list[str]) -> None:
    if isinstance(node, ast.ImportFrom) and node.level:
        errors.append("relative imports are not allowed")
        return
    modules = (
        [alias.name for alias in node.names]
        if isinstance(node, ast.Import)
        else [node.module or ""]
    )
    for module in modules:
        if not any(
            module == allowed or module.startswith(f"{allowed}.")
            for allowed in _ALLOWED_IMPORTS
        ):
            errors.append(f"import from {module or '<unknown>'} is not allowed")
    if isinstance(node, ast.ImportFrom) and node.module == "odmantic":
        if any(
            alias.name not in {"Field", "Model", "EmbeddedModel"}
            for alias in node.names
        ):
            errors.append(
                "only Field, Model, and EmbeddedModel may be imported from odmantic"
            )
    if isinstance(node, ast.ImportFrom) and node.module == "simstack.core.node":
        if any(alias.name != "node" for alias in node.names):
            errors.append("only node may be imported from simstack.core.node")


def _validate_decorator(
    decorator: ast.expr,
    errors: list[str],
    decorator_bindings: dict[str, str],
) -> None:
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    name = (_qualified_name(target) or "").rsplit(".", 1)[-1]
    is_allowed = isinstance(target, ast.Name) and name in decorator_bindings
    if not is_allowed:
        errors.append(f"decorator {name or '<dynamic>'} is not allowed")
        return
    if call is not None and (
        any(not _is_literal(arg) for arg in call.args)
        or any(
            keyword.arg is None or not _is_literal(keyword.value)
            for keyword in call.keywords
        )
    ):
        errors.append(f"decorator {name} arguments must be deterministic literals")


def _validate_definition_time_expressions(tree: ast.Module, errors: list[str]) -> None:
    decorator_bindings = generated_workflow_decorator_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                _validate_decorator(decorator, errors, decorator_bindings)
            defaults = [
                *node.args.defaults,
                *(item for item in node.args.kw_defaults if item),
            ]
            if any(not _is_literal(default) for default in defaults):
                errors.append("function defaults must be deterministic literals")
        elif isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                _validate_decorator(decorator, errors, decorator_bindings)
            if _uses_trusted_decorator(
                node,
                "simstack_model",
                decorator_bindings,
            ):
                for statement in node.body:
                    if isinstance(statement, ast.AnnAssign) and any(
                        isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr)
                        for item in ast.walk(statement.annotation)
                    ):
                        field_name = (
                            statement.target.id
                            if isinstance(statement.target, ast.Name)
                            else "<dynamic>"
                        )
                        errors.append(
                            f"@simstack_model field {node.name}.{field_name} cannot "
                            "use a PEP 604 union; use Optional[T] and "
                            "Field(default=None)"
                        )
            if node.keywords or any(
                not isinstance(base, (ast.Name, ast.Attribute)) for base in node.bases
            ):
                errors.append("dynamic class bases and metaclasses are not allowed")
            for statement in node.body:
                value = (
                    statement.value
                    if isinstance(statement, (ast.Assign, ast.AnnAssign))
                    else None
                )
                if isinstance(value, ast.Call):
                    name = (_qualified_name(value.func) or "").rsplit(".", 1)[-1]
                    if (
                        name != "Field"
                        or any(not _is_literal(arg) for arg in value.args)
                        or any(
                            keyword.arg is None or not _is_literal(keyword.value)
                            for keyword in value.keywords
                        )
                    ):
                        errors.append(
                            "class field calls must be Field with literal arguments"
                        )


def _uses_trusted_decorator(
    definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    decorator_name: str,
    decorator_bindings: dict[str, str],
) -> bool:
    for decorator in definition.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(target, ast.Name)
            and decorator_bindings.get(target.id) == decorator_name
        ):
            return True
    return False


def _registered_node_input_names(
    tree: ast.Module,
    decorator_bindings: dict[str, str],
) -> set[str]:
    """Find model names that can resolve to a registered SimStack input mapping.

    Generated local models are accepted only when they use the trusted
    ``@simstack_model`` decorator and directly inherit the exact unshadowed
    ``Model`` imported from ``odmantic``. Existing models must be imported
    explicitly from ``simstack.models`` (or one of its submodules). Restricting
    annotations to these direct names keeps the check static: validating
    generated code must never import or execute it.
    """

    bound_name_counts = _bound_name_counts(tree)
    odmantic_model_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and not statement.level
        and statement.module == "odmantic"
        for alias in statement.names
        if alias.name == "Model" and bound_name_counts[alias.asname or alias.name] == 1
    }
    names = {
        statement.name
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
        and bound_name_counts[statement.name] == 1
        and len(statement.bases) == 1
        and isinstance(statement.bases[0], ast.Name)
        and statement.bases[0].id in odmantic_model_names
        and _uses_trusted_decorator(
            statement,
            "simstack_model",
            decorator_bindings,
        )
    }
    for statement in tree.body:
        if (
            not isinstance(statement, ast.ImportFrom)
            or statement.level
            or not (
                statement.module == "simstack.models"
                or (statement.module or "").startswith("simstack.models.")
            )
        ):
            continue
        for alias in statement.names:
            bound_name = alias.asname or alias.name
            if (
                alias.name != "*"
                and bound_name_counts[bound_name] == 1
                and _TRUSTED_DECORATOR_IMPORTS.get((statement.module or "", alias.name))
                is None
            ):
                names.add(bound_name)
    return names


def _simple_annotation_name(annotation: ast.expr) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if (
        isinstance(annotation, ast.Constant)
        and isinstance(annotation.value, str)
        and annotation.value.isidentifier()
    ):
        return annotation.value
    return None


def _validate_node_input_models(tree: ast.Module, errors: list[str]) -> None:
    """Require every persisted ``@node`` input to be a SimStack model.

    ``CreateNodeTable`` derives an input mapping from each annotation's class
    module and name. Raw primitives, containers, unions, and typing wrappers
    therefore produce mappings such as ``builtins.float`` or ``builtins.list``
    that cannot be rendered or submitted. ``**kwargs`` is runtime plumbing and
    is intentionally not a persisted input mapping.
    """

    decorator_bindings = generated_workflow_decorator_bindings(tree)
    registered_names = _registered_node_input_names(tree, decorator_bindings)
    for definition in ast.walk(tree):
        if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _uses_trusted_decorator(definition, "node", decorator_bindings):
            continue

        arguments = definition.args
        persisted_inputs = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg is not None:
            persisted_inputs.append(arguments.vararg)
        for parameter in persisted_inputs:
            annotation_name = (
                _simple_annotation_name(parameter.annotation)
                if parameter.annotation is not None
                else None
            )
            if annotation_name in registered_names:
                continue
            annotation = (
                ast.unparse(parameter.annotation)
                if parameter.annotation is not None
                else "<missing>"
            )
            errors.append(
                f"@node parameter {definition.name}.{parameter.arg} annotation "
                f"{annotation[:160]} is not a registered SimStack model; use a "
                "local @simstack_model input class directly inheriting imported "
                "odmantic.Model or an explicitly imported simstack.models model"
            )


def validate_generated_workflow_source(
    source_code: str,
    *,
    require_registered_node_inputs: bool = True,
) -> ast.Module:
    """Reject generated source capable of escaping the workflow runtime boundary."""

    if len(source_code.encode("utf-8")) > 256 * 1024:
        raise GeneratedWorkflowValidationError(["Generated source exceeds 256 KiB"])
    try:
        tree = ast.parse(source_code, mode="exec")
        compile(tree, "<generated-workflow>", "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        raise GeneratedWorkflowValidationError(
            [f"Generated Python is invalid: {exc}"]
        ) from exc

    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _validate_import(node, errors)
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith("__") or node.attr in _FORBIDDEN_METHODS
        ):
            errors.append(f"attribute {node.attr} is not allowed")
        elif isinstance(node, ast.Name) and (
            node.id.startswith("__") or node.id in _FORBIDDEN_CALLS
        ):
            errors.append(f"reference to {node.id} is not allowed")
        elif isinstance(node, ast.Call):
            name = (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
            if name in _FORBIDDEN_CALLS or name in _FORBIDDEN_METHODS:
                errors.append(f"call to {name} is not allowed")

    _validate_definition_time_expressions(tree, errors)
    if require_registered_node_inputs:
        _validate_node_input_models(tree, errors)
    if errors:
        raise GeneratedWorkflowValidationError(errors)
    return tree
