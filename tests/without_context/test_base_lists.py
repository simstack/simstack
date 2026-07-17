from simstack.models import BooleanData, StringData
from simstack.models.base_lists import BooleanDataList, StringDataList


def test_object_data_lists_resolve_runtime_model_classes():
    assert StringDataList()._get_model_class() is StringData
    assert BooleanDataList()._get_model_class() is BooleanData
