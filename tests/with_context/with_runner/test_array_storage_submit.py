import json

import numpy as np
import pytest

from simstack.core.node import node
from simstack.models import FloatData, Parameters
from simstack.models.array_storage import ArrayStorage


@node
def sum_direct_array_upload_in_tests(
    array_storage: ArrayStorage, **kwargs
) -> FloatData:
    return FloatData(value=float(array_storage.get_array().sum()))


def test_direct_array_storage_payload_starts_lightweight_workflow():
    array = np.array([[1.0, 2.0], [3.0, 4.0]])
    upload_payload = {
        "name": "curve",
        "field_name": "curve",
        "shape": "2,2",
        "data_json": json.dumps(array.flatten().tolist()),
    }

    storage = ArrayStorage.from_dict(upload_payload)
    result = sum_direct_array_upload_in_tests(
        storage,
        parameters=Parameters(resource="test", force_rerun=True),
    )

    assert result.value == pytest.approx(10.0)
