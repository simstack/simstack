import functools
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from simstack.core.node import node
from simstack.core.node_code_version import compute_node_code_version, node_code_version


def _function(source, *, filename="workflow.py", module="example.workflows"):
    namespace = {"__name__": module}
    exec(compile(source, filename, "exec", dont_inherit=True), namespace)
    return namespace["task"]


def test_same_code_reuses_identity_across_source_formatting_and_location():
    first = _function('def task(value):\n    "First documentation"\n    return value + 1\n')
    second = _function('\n\n# A new comment\ndef task(value):\n    "New documentation"\n    return value+1\n', filename="elsewhere/changed.py")
    assert compute_node_code_version(first) == compute_node_code_version(second)


@pytest.mark.parametrize("source", [
    "def task(value=1): return value + 2",
    "def task(value=2): return value + 1",
    "def task(value=1): return (lambda: value + 2)()",
])
def test_changed_code_constants_or_defaults_change_identity(source):
    first = _function("def task(value=1): return value + 1")
    assert compute_node_code_version(first) != compute_node_code_version(_function(source))


def test_module_identity_and_declared_version_are_part_of_cache_key():
    source = "def task(value): return value + 1"
    first = node(version="one")(_function(source))
    other_module = node(version="one")(_function(source, module="another.workflows"))
    other_version = node(version="two")(_function(source))
    assert node_code_version(first._inner) != node_code_version(other_module._inner)
    assert node_code_version(first._inner) != node_code_version(other_version._inner)


def test_source_layout_import_alias_has_the_same_code_identity():
    canonical = importlib.import_module("simstack.methods.upload_helpers")
    alias = importlib.import_module("src.simstack.methods.upload_helpers")
    assert Path(canonical.__file__).resolve() == Path(alias.__file__).resolve()
    assert compute_node_code_version(canonical.archive_upload._inner) == compute_node_code_version(alias.archive_upload._inner)


def test_source_prefix_is_not_removed_for_distinct_module_files(tmp_path, monkeypatch):
    functions = []
    for name, filename in (("separate_workflow", "canonical.py"), ("src.separate_workflow", "alias.py")):
        source = tmp_path / filename
        source.write_text("def task(value): return value + 1\n")
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        functions.append(module.task)
    assert compute_node_code_version(functions[0]) != compute_node_code_version(functions[1])


def test_changing_defaults_invalidates_cached_identity():
    task = _function("def task(value=1, *, offset=2): return value + offset")
    first = node_code_version(task)
    task.__defaults__ = (3,)
    second = node_code_version(task)
    task.__kwdefaults__["offset"] = 4
    assert first != second
    assert second != node_code_version(task)


def test_closure_callable_implementation_changes_identity():
    def factory(operation):
        def task(value):
            return operation(value)
        return task
    first = factory(lambda value: value + 1)
    second = factory(lambda value: value + 2)
    assert compute_node_code_version(first) != compute_node_code_version(second)


def test_captured_runtime_state_does_not_change_declared_implementation_snapshot():
    calls = []
    def task():
        calls.append(1)
        return 1
    first = node_code_version(task)
    task()
    assert node_code_version(task) == first


def test_order_and_aliasing_of_captured_values_are_preserved():
    def ordered_factory(options):
        def task():
            return next(iter(options))
        return task
    first = ordered_factory({"a": 1, "b": 2})
    second = ordered_factory({"b": 2, "a": 1})
    assert compute_node_code_version(first) != compute_node_code_version(second)

    def alias_factory(left, right):
        def task():
            return left is right
        return task
    shared = []
    assert compute_node_code_version(alias_factory(shared, shared)) != compute_node_code_version(alias_factory([], []))


def test_partial_and_path_defaults_have_stable_identity():
    def operation(path, offset=0):
        return str(path), offset
    def factory():
        operation_with_default = functools.partial(operation, Path("inputs"), offset=1)
        def task():
            return operation_with_default()
        return task
    assert compute_node_code_version(factory()) == compute_node_code_version(factory())


def test_opaque_dependency_can_be_versioned_explicitly():
    external = object()
    def task():
        return external
    with pytest.raises(TypeError, match="@node"):
        node_code_version(task)
    decorated = node(version="dependency-v1")(task)
    assert node_code_version(decorated._inner)


def test_identity_is_stable_across_python_hash_seeds():
    script = '''from simstack.core.node_code_version import compute_node_code_version
def factory():
    shared = ("common",)
    options = {("apple", shared), ("banana", shared), ("cherry", shared), ("date", shared)}
    def task(value=None):
        return len(options), value in {"one", "two", "three"}
    return task
task = factory()
print(compute_node_code_version(task))
'''
    results = []
    for seed in ("1", "3", "4", "5"):
        result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                                text=True, env={**os.environ, "PYTHONHASHSEED": seed})
        results.append(result.stdout.strip())
    assert len(set(results)) == 1
