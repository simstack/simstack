import pytest

from simstack.models import FloatData
from simstack.core.context import context
from simstack.persistence import eq


@pytest.mark.asyncio
async def test_load_float_data():
    for i in range(10):
        float_value = FloatData(value=i)
        await context.repository.save(float_value)
    float_list = await context.repository.find(FloatData)
    assert len(float_list) == 10
    float_id = float_list[-1].id
    float_data = await context.repository.find_one(FloatData, eq("id", float_id))
    assert float_data.value == pytest.approx(float_value.value)
