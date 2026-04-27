import pytest

from simstack.models import FloatData
from simstack.core.context import context


@pytest.mark.asyncio
async def test_load_float_data():
    for i in range(10):
        float_value = FloatData(value=i)
        await context.db.save(float_value)
    float_list = await context.db.engine.find(FloatData, {})
    assert len(float_list) == 10
    float_id = float_list[-1].id
    float_data = await context.db.engine.find_one(FloatData, FloatData.id == float_id)
    assert float_data.value == pytest.approx(float_value.value)
    float_data = await context.db.find_one_by_model_name("FloatData", float_id)
    print("FloatData: ", float_data)
