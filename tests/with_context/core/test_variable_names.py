import pytest
from simstack.core.node import Node, node
from simstack.models import FloatData, Parameters, NamedDataReference, Project
from simstack.core.context import context


@node
def my_test_node(first_param: FloatData, second_param: FloatData) -> FloatData:
    return FloatData(value=first_param.value + second_param.value)


@node
def var_args_node(*args: FloatData) -> FloatData:
    return FloatData(value=sum(a.value for a in args))


@pytest.mark.asyncio
async def test_make_registry_entry_variable_names(initialized_context):
    data1, data2 = FloatData(value=1), FloatData(value=2)
    node_instance = Node(data1, data2, func=my_test_node._inner, is_async=False, parameters=Parameters())
    entry = await node_instance.make_registry_entry("func_hash", "arg_hash")
    assert [ref.variable_name for ref in entry.input_references] == ["first_param", "second_param"]
    resolved = await context.server.values.resolve_values(entry.input_references)
    assert [value.id for value in resolved] == [data1.id, data2.id]


@pytest.mark.asyncio
async def test_make_registry_entry_var_args(initialized_context):
    data1, data2 = FloatData(value=1), FloatData(value=2)
    node_instance = Node(data1, data2, func=var_args_node._inner, is_async=False, parameters=Parameters())
    entry = await node_instance.make_registry_entry("func_hash", "arg_hash")
    assert [ref.variable_name for ref in entry.input_references] == ["args", "arg_1"]
    resolved = await context.server.values.resolve_values(entry.input_references)
    assert [value.id for value in resolved] == [data1.id, data2.id]


@pytest.mark.asyncio
async def test_named_data_reference_from_variable_field_name(initialized_context):
    project = Project(field_name="my_project")
    reference = NamedDataReference.from_variable(project, variable_name=None)
    assert reference.variable_name == "my_project"
    assert reference.reference == project.id
    data = FloatData(value=1)
    reference = NamedDataReference.from_variable(data, variable_name=None)
    assert reference.variable_name == "float"
    assert reference.reference == data.id
