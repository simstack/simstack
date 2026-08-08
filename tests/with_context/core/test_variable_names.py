import pytest
from simstack.core.node import Node, node
from simstack.models import FloatData, Parameters, ModelMapping, NodeModel, NamedDataReference
from simstack.core.context import context

@node
def my_test_node(first_param: FloatData, second_param: FloatData) -> FloatData:
    return FloatData(value=first_param.value + second_param.value)

@pytest.mark.asyncio
async def test_make_registry_entry_variable_names(initialized_context):
    """Test that make_registry_entry uses correct variable names from the function signature."""
    # Setup mappings
    node_model = NodeModel(
        name="my_test_node",
        function_mapping="simstack_tests.with_context.core.test_variable_names.my_test_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    
    model_mapping = ModelMapping(
        name="FloatData",
        mapping="simstack.models.FloatData",
        collection_name="float_data"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()

    try:
        data1 = FloatData(value=1.0)
        data2 = FloatData(value=2.0)
        
        # Initialize Node
        n = Node(data1, data2, func=my_test_node._inner, is_async=False, parameters=Parameters())
        
        # Create registry entry
        entry = await n.make_registry_entry("func_hash", "arg_hash")
        
        try:
            assert len(entry.input_references) == 2
            assert entry.input_references[0].variable_name == "first_param"
            assert entry.input_references[1].variable_name == "second_param"
        finally:
            await context.db.delete(entry)
            await context.db.delete(data1)
            await context.db.delete(data2)
    finally:
        await context.db.delete(node_model)
        await context.db.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_make_registry_entry_var_args(initialized_context):
    """Test that make_registry_entry handles *args by using arg_i naming."""
    @node
    def var_args_node(*args: FloatData) -> FloatData:
        return FloatData(value=sum(a.value for a in args))

    node_model = NodeModel(
        name="var_args_node",
        function_mapping="simstack_tests.with_context.core.test_variable_names.test_make_registry_entry_var_args.<locals>.var_args_node",
        input_mappings=[],
        default_parameters=Parameters()
    )
    await context.db.save(node_model)
    
    model_mapping = ModelMapping(
        name="FloatData",
        mapping="simstack.models.FloatData",
        collection_name="float_data"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()

    try:
        data1 = FloatData(value=1.0)
        data2 = FloatData(value=2.0)
        
        n = Node(data1, data2, func=var_args_node._inner, is_async=False, parameters=Parameters())
        entry = await n.make_registry_entry("func_hash", "arg_hash")
        
        try:
            assert len(entry.input_references) == 2
            # Since *args is one parameter 'args', but we have 2 arguments.
            # param_names will be ['args']
            # i=0: param_names[0] -> 'args'
            # i=1: arg_1
            assert entry.input_references[0].variable_name == "args"
            assert entry.input_references[1].variable_name == "arg_1"
        finally:
            await context.db.delete(entry)
            await context.db.delete(data1)
            await context.db.delete(data2)
    finally:
        await context.db.delete(node_model)
        await context.db.delete(model_mapping)
        await context.refresh_mappings()

@pytest.mark.asyncio
async def test_named_data_reference_from_variable_field_name(initialized_context):
    """Test that NamedDataReference.from_variable uses field_name if variable_name is None."""
    from simstack.models import Project
    
    # Project has field_name
    project = Project(field_name="my_project")
    await context.db.save(project)
    
    model_mapping = ModelMapping(
        name="Project",
        mapping="simstack.models.Project",
        collection_name="project"
    )
    await context.db.save(model_mapping)
    await context.refresh_mappings()

    try:
        ref = NamedDataReference.from_variable(project, variable_name=None)
        assert ref.variable_name == "my_project"
        
        # Test default "variable" when no field_name
        data = FloatData(value=1.0)
        await context.db.save(data)
        model_mapping2 = ModelMapping(
            name="FloatData",
            mapping="simstack.models.FloatData",
            collection_name="float_data"
        )
        await context.db.save(model_mapping2)
        await context.refresh_mappings()
        
        ref2 = NamedDataReference.from_variable(data, variable_name=None)
        # FloatData has default field_name="float"
        assert ref2.variable_name == "float"
        
        await context.db.delete(data)
        await context.db.delete(model_mapping2)
    finally:
        await context.db.delete(project)
        await context.db.delete(model_mapping)
        await context.refresh_mappings()
