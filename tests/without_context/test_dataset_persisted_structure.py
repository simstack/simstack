from odmantic import ObjectId

from simstack.models.dataset import DataSet, DataSetSection
from simstack.models.dataset_metadata import DataSetMetadata


def test_collect_structure_uses_persisted_section_data_without_cache():
    section = DataSetSection(
        model_types={"curve": "ArrayStorage"},
        data={"row-1": {"curve": ObjectId()}},
    )
    dataset = DataSet(
        metadata=DataSetMetadata(field_name="curves", data={}),
        sections={"input": section},
    )

    assert section._get_cache() == {}
    assert dataset.collect_structure() == {
        "input": {"curve": "ArrayStorage"},
    }
