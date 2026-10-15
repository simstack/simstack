"""Regression evidence for argument hashes crossing API worker processes."""

import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from simstack.core.hash import complex_hash_function
from simstack.core.node import compute_arg_hash
from simstack.core.runner_execution.nested import _nested_input_values, _root_call_arguments
from simstack.models.array_storage import ArrayStorage
from simstack.models.base_types import IntData
from simstack.models.document import decode_document_value, encode_document_value
from simstack.models.pandas_model import PandasModel


def inputs():
    array = ArrayStorage(name="curves")
    array.array = np.array([[1.0, 2.0], [3.0, 4.0]])
    table = PandasModel.from_data_frame(pd.DataFrame({"parameter": [1, 2]}))
    return [array, table, IntData(value=0), IntData(value=2)]


def process(curves: ArrayStorage, parameters: PandasModel, start: IntData, end: IntData, **kwargs):
    return curves, parameters, start, end


@pytest.mark.parametrize("factory", [bytes, bytearray])
def test_binary_hash_covers_all_bytes(factory):
    original = factory(b"payload" * 2000)
    changed = factory(bytes(original[:-1]) + b"X")
    assert complex_hash_function(original) == complex_hash_function(factory(original))
    assert complex_hash_function(original) != complex_hash_function(changed)
    assert complex_hash_function(original) != complex_hash_function(bytes(original).decode())


def test_same_process_wire_roundtrip_and_mixed_binding_preserve_hash():
    original = inputs()
    values = _nested_input_values(process, original[:2], {"start": original[2], "end": original[3], "node_runner": object(), "task_id": "runtime"})
    assert [name for name, _ in values] == ["curves", "parameters", "start", "end"]
    wire = json.loads(json.dumps([encode_document_value(value.model_dump_doc()) for value in original]))
    reconstructed = [type(value).model_validate_doc(decode_document_value(document)) for value, document in zip(original, wire)]
    assert compute_arg_hash(original) == compute_arg_hash(reconstructed)
    args, kwargs = _root_call_arguments(process, list(zip([name for name, _ in values], reconstructed)))
    assert args == []
    assert list(kwargs) == ["curves", "parameters", "start", "end"]


@pytest.mark.parametrize("case", ["bytes", "none", "pandas", "combined", "array"])
def test_hash_is_identical_in_fresh_worker_processes(case):
    original = inputs()
    parent = {
        "bytes": complex_hash_function(b"fixed content"),
        "none": complex_hash_function(None),
        "pandas": compute_arg_hash([original[1]]),
        "combined": compute_arg_hash(original),
        "array": compute_arg_hash([original[0]]),
    }
    wire = json.dumps([encode_document_value(value.model_dump_doc()) for value in original])
    child_script = '''
import json, sys
from simstack.core.hash import complex_hash_function
from simstack.core.node import compute_arg_hash
from simstack.models.array_storage import ArrayStorage
from simstack.models.pandas_model import PandasModel
from simstack.models.base_types import IntData
from simstack.models.document import decode_document_value
types = [ArrayStorage, PandasModel, IntData, IntData]
models = [kind.model_validate_doc(decode_document_value(document)) for kind, document in zip(types, json.loads(sys.stdin.read()))]
print(json.dumps({"bytes":complex_hash_function(b"fixed content"),"none":complex_hash_function(None),"pandas":compute_arg_hash([models[1]]),"combined":compute_arg_hash(models),"array":compute_arg_hash([models[0]])}))
'''
    observations = [json.loads(subprocess.check_output([sys.executable, "-c", child_script], input=wire, text=True)) for _ in range(3)]
    assert all(row[case] == parent[case] for row in observations), {"case": case, "parent": parent[case], "workers": [row[case] for row in observations]}
