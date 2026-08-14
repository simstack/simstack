from __future__ import annotations

import pytest

from simstack.core.generated_workflow_validation import (
    GeneratedWorkflowValidationError,
    validate_generated_workflow_source,
)


@pytest.mark.parametrize(
    "model_import",
    [
        "from simstack.models import simstack_model as aiwf_simstack_model",
        (
            "from simstack.models.simstack_model import "
            "simstack_model as aiwf_simstack_model"
        ),
    ],
)
def test_trusted_decorator_import_aliases_are_allowed(model_import: str):
    source = f"""from odmantic import Model
from simstack.core.node import node as aiwf_node
{model_import}

@aiwf_simstack_model
class aiwf_Result(Model):
    value: int

@aiwf_node(expose_in_submit=True)
def aiwf_run(**kwargs) -> aiwf_Result:
    return aiwf_Result(value=1)
"""

    validate_generated_workflow_source(source)


@pytest.mark.parametrize(
    "annotation, imports",
    [
        ("float", ""),
        ("list[str]", ""),
        ("dict[str, float]", ""),
        ("float | None", ""),
        (
            "Optional[FloatData]",
            "from typing import Optional\nfrom simstack.models import FloatData",
        ),
        ("<missing>", ""),
    ],
)
def test_node_inputs_reject_unregistered_builtin_container_and_union_annotations(
    annotation: str,
    imports: str,
):
    parameter = "value" if annotation == "<missing>" else f"value: {annotation}"
    source = f"""from simstack.core.node import node
{imports}

@node(expose_in_submit=True)
def aiwf_run({parameter}, **kwargs):
    return kwargs["node_runner"].succeed()
"""

    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(source)

    assert any(
        error.startswith("@node parameter aiwf_run.value annotation ")
        and "is not a registered SimStack model" in error
        for error in raised.value.errors
    )


def test_node_inputs_accept_local_and_explicitly_imported_simstack_models():
    source = """from odmantic import Model
from simstack.core.node import node
from simstack.models import FloatData, simstack_model

@simstack_model
class aiwf_Inputs(Model):
    value: float

@node(expose_in_submit=False)
def aiwf_helper(value: FloatData, **kwargs):
    return kwargs["node_runner"].succeed()

@node(expose_in_submit=True)
def aiwf_run(inputs: aiwf_Inputs, **kwargs: dict[str, object]):
    return aiwf_helper(FloatData(value=inputs.value))
"""

    validate_generated_workflow_source(source)


def test_simstack_model_fields_reject_pep604_union_and_accept_optional():
    invalid_source = """from odmantic import Field, Model
from simstack.models import simstack_model

@simstack_model
class aiwf_Inputs(Model):
    upper_bound: float | None = Field(default=None)
"""

    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(invalid_source)

    assert (
        "@simstack_model field aiwf_Inputs.upper_bound cannot use a PEP 604 "
        "union; use Optional[T] and Field(default=None)" in raised.value.errors
    )

    valid_source = """from typing import Optional
from odmantic import Field, Model
from simstack.models import simstack_model

@simstack_model
class aiwf_Inputs(Model):
    upper_bound: Optional[float] = Field(default=None)
"""

    validate_generated_workflow_source(valid_source)


def test_node_inputs_reject_plain_local_model_without_simstack_model_decorator():
    source = """from odmantic import Model
from simstack.core.node import node

class aiwf_Inputs(Model):
    value: float

@node(expose_in_submit=True)
def aiwf_run(inputs: aiwf_Inputs, **kwargs):
    return kwargs["node_runner"].succeed()
"""

    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(source)

    assert any(
        "aiwf_run.inputs annotation aiwf_Inputs is not a registered SimStack model"
        in error
        for error in raised.value.errors
    )


@pytest.mark.parametrize(
    "model_import, base",
    [
        ("", ""),
        ("from odmantic import EmbeddedModel", "(EmbeddedModel)"),
    ],
)
def test_node_inputs_require_local_model_to_directly_inherit_odmantic_model(
    model_import: str,
    base: str,
):
    source = f"""{model_import}
from simstack.core.node import node
from simstack.models import simstack_model

@simstack_model
class aiwf_Inputs{base}:
    value: float

@node(expose_in_submit=True)
def aiwf_run(inputs: aiwf_Inputs, **kwargs):
    return kwargs["node_runner"].succeed()
"""

    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(source)

    assert any(
        "aiwf_run.inputs annotation aiwf_Inputs is not a registered SimStack model"
        in error
        for error in raised.value.errors
    )


def test_skipping_node_input_check_does_not_skip_security_validation():
    source = """from simstack.core.node import node

@node(expose_in_submit=True)
def aiwf_run(value: float, **kwargs):
    return getattr(value, "real")
"""

    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(
            source,
            require_registered_node_inputs=False,
        )

    assert "call to getattr is not allowed" in raised.value.errors
    assert not any(
        "is not a registered SimStack model" in error for error in raised.value.errors
    )


@pytest.mark.parametrize(
    "source",
    [
        """from simstack.core.node import node

@aiwf_node
def aiwf_run(**kwargs):
    return True
""",
        """from simstack.core.node import node as aiwf_node

def aiwf_node(function):
    return function

@aiwf_node
def aiwf_run(**kwargs):
    return True
""",
        """from simstack.models.base_types import FloatData as aiwf_node

@aiwf_node
def aiwf_run(**kwargs):
    return True
""",
        """def node(function):
    return function

@node
def aiwf_run(**kwargs):
    return True
""",
        """import simstack.core.node

@simstack.core.node.node
def aiwf_run(**kwargs):
    return True
""",
    ],
)
def test_decorator_alias_must_be_one_unshadowed_exact_trusted_import(source: str):
    with pytest.raises(
        GeneratedWorkflowValidationError,
        match=r"decorator (aiwf_node|node) is not allowed",
    ):
        validate_generated_workflow_source(source)


@pytest.mark.parametrize(
    "source, message",
    [
        (
            """def aiwf_run(value):
    return hasattr(value, "field")
""",
            "call to hasattr is not allowed",
        ),
        (
            """def aiwf_run(value):
    checker = hasattr
    return checker(value, "field")
""",
            "reference to hasattr is not allowed",
        ),
    ],
)
def test_dynamic_attribute_introspection_is_rejected(source: str, message: str):
    with pytest.raises(GeneratedWorkflowValidationError) as raised:
        validate_generated_workflow_source(source)

    assert message in raised.value.errors
