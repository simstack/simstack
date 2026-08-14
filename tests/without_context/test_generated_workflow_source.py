from __future__ import annotations

import sys
from pathlib import Path

import pytest

from simstack.core.generated_workflow import (
    canonical_generated_module_name,
    canonical_source_sha256,
    generated_module_path,
    import_materialized_generated_module,
    materialize_generated_workflow_source,
)
from simstack.models.generated_workflow import GeneratedWorkflowSource


SOURCE_TEMPLATE = """from odmantic import Model

class SharedResult(Model):
    value: int

def entrypoint():
    return SharedResult(value={value})
"""


def _source(
    source_code: str,
    *,
    workflow_id: str = "stress-strain",
    revision: int = 1,
) -> GeneratedWorkflowSource:
    source_sha256 = canonical_source_sha256(source_code)
    return GeneratedWorkflowSource(
        workflow_id=workflow_id,
        revision=revision,
        title="Stress strain",
        namespace="simstack_generated",
        module_name=canonical_generated_module_name(
            workflow_id,
            revision,
            source_sha256,
        ),
        entrypoint_name="entrypoint",
        source_code=source_code,
        source_sha256=source_sha256,
        target_resource="runner-a",
    )


def test_materialize_generated_source_is_atomic_and_idempotent(tmp_path: Path):
    source = _source(SOURCE_TEMPLATE.format(value=1))

    first = materialize_generated_workflow_source(source, root=tmp_path)
    second = materialize_generated_workflow_source(source, root=tmp_path)

    assert first == second
    assert first.file_path.read_text() == source.source_code
    assert (
        first.file_path == tmp_path / "simstack_generated" / f"{source.module_name}.py"
    )
    assert not list(first.file_path.parent.glob(f".{first.file_path.name}.*.tmp"))


def test_materialize_rejects_hash_and_module_name_mismatches(tmp_path: Path):
    source = _source(SOURCE_TEMPLATE.format(value=1))
    source.source_sha256 = "0" * 64
    with pytest.raises(ValueError, match="does not match source_sha256"):
        materialize_generated_workflow_source(source, root=tmp_path)

    source = _source(SOURCE_TEMPLATE.format(value=1))
    source.module_name = "wrong"
    with pytest.raises(ValueError, match="module_name must be"):
        materialize_generated_workflow_source(source, root=tmp_path)


@pytest.mark.parametrize(
    "namespace",
    ["other", "simstack_generated...bad", "simstack_generated.bad-name", "../bad"],
)
def test_materialize_rejects_unsafe_namespaces(tmp_path: Path, namespace: str):
    source = _source(SOURCE_TEMPLATE.format(value=1))
    source.namespace = namespace

    with pytest.raises(ValueError, match="namespace must be"):
        materialize_generated_workflow_source(source, root=tmp_path)


def test_two_revisions_with_same_class_name_import_exact_modules(tmp_path: Path):
    revision_one = _source(SOURCE_TEMPLATE.format(value=1), revision=1)
    revision_two = _source(SOURCE_TEMPLATE.format(value=2), revision=2)

    module_one = import_materialized_generated_module(revision_one, root=tmp_path)
    module_two = import_materialized_generated_module(revision_two, root=tmp_path)

    assert module_one.SharedResult.__name__ == module_two.SharedResult.__name__
    assert module_one.SharedResult is not module_two.SharedResult
    assert module_one.entrypoint().value == 1
    assert module_two.entrypoint().value == 2
    assert module_one.entrypoint._simstack_source_revision == revision_one.id
    assert module_two.entrypoint._simstack_source_revision == revision_two.id
    assert generated_module_path(revision_one) in sys.modules
    assert generated_module_path(revision_two) in sys.modules
