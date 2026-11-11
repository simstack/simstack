import pytest

from simstack.models import FloatData
from simstack.core.context import context


@pytest.mark.asyncio
async def test_load_float_data():
    for i in range(10):
        float_value = FloatData(value=i)
        await context.server.values.publish_value(float_value)
    float_list = await context.server.values.query_records(FloatData, "all")
    assert len(float_list) == 10
    float_id = float_list[-1].id
    float_data = await context.server.values.get_value(FloatData, float_id)
    assert float_data.value == pytest.approx(float_value.value)
    float_data = await context.server.values.get_value(FloatData, float_id)
    print("FloatData: ", float_data)
