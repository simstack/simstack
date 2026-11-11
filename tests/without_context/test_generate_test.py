from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, call
import importlib.util
import json

import pytest
from simstack.models.document import ObjectId

import simstack.methods.generate_test as generate_test_module
from simstack.models import FloatData, ModelMapping, NamedDataReference
from simstack.models import NodeRegistry, Parameters, StringData
from simstack.server_api.http_transport import RunnerAPIError


@pytest.mark.asyncio
async def test_load_models_preserves_named_reference_key(monkeypatch):
    model = FloatData(value=4.2)

    class FakeScientificValuesAPI:
        def __init__(self):
            self.mapping_lookups = 0

        async def get_value(self, mapping, identity):
            return model

        async def catalogue_models(self):
            self.mapping_lookups += 1
            return []

    async def fake_import_class(mapping, db):
        return FloatData

    api = FakeScientificValuesAPI()
    monkeypatch.setattr(generate_test_module, "context", SimpleNamespace(server=SimpleNamespace(values=api, tasks=api)))
    monkeypatch.setattr(generate_test_module, "import_class", fake_import_class)

    reference = NamedDataReference(
        variable_name="curve_sum",
        variable_mapping="simstack.models.FloatData",
        reference=ObjectId(),
    )
    loaded = await generate_test_module.load_models([reference])

    assert loaded == {"curve_sum": model}
    assert api.mapping_lookups == 0


@pytest.mark.asyncio
async def test_load_models_fails_when_reference_is_missing(monkeypatch):
    class FakeScientificValuesAPI:
        async def get_value(self, mapping, identity):
            raise LookupError("Missing scientific value")

    async def fake_import_class(mapping, db):
        return FloatData

    monkeypatch.setattr(
        generate_test_module,
        "context",
        SimpleNamespace(server=SimpleNamespace(values=FakeScientificValuesAPI())),
    )
    monkeypatch.setattr(generate_test_module, "import_class", fake_import_class)

    reference = NamedDataReference(
        variable_name="curve_sum",
        variable_mapping="simstack.models.FloatData",
        reference=ObjectId(),
    )

    with pytest.raises(RuntimeError, match="Failed to load"):
        await generate_test_module.load_models([reference])


@pytest.mark.asyncio
async def test_packaged_generator_exports_frozen_inputs_and_outputs(monkeypatch, tmp_path):
    frozen, current = FloatData(value=1), FloatData(value=2)
    reference = NamedDataReference(variable_name="sample", variable_mapping="simstack.models.base_types.FloatData", reference=frozen.id)
    entry = NodeRegistry(name="probe", status="completed", arg_hash="frozen-input", function_hash="",
        func_mapping="fixture.probe", parameters=Parameters(), input_references=[reference], results_references=[reference])
    values = SimpleNamespace(resolve_task_inputs=AsyncMock(return_value=[frozen]),
        resolve_task_outputs=AsyncMock(return_value=[frozen]), get_value=AsyncMock(return_value=current))
    monkeypatch.setattr(generate_test_module, "context", SimpleNamespace(
        server=SimpleNamespace(tasks=SimpleNamespace(get_task=AsyncMock(return_value=entry)), values=values),
        config=SimpleNamespace(workdir=tmp_path / "work")))
    monkeypatch.setattr(generate_test_module, "import_class", AsyncMock(return_value=FloatData))
    await generate_test_module.generate_test(str(entry.id), tmp_path / "export")
    assert json.loads((tmp_path / "export/probe/frozen-input/inputs.json").read_text())["sample"]["value"] == 1
    assert json.loads((tmp_path / "export/probe/frozen-input/outputs.json").read_text())["sample"]["value"] == 1
    values.resolve_task_outputs.assert_awaited_once_with(entry)
    values.get_value.assert_not_awaited()


@pytest.mark.parametrize("script,include_node_name", [
    ("scripts/generate_tests.py", True),
    ("tests/extract_node_data.py", False),
])
@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.asyncio
async def test_standalone_exports_keep_layout_files_and_order_through_api(
    monkeypatch, tmp_path, script, include_node_name, initialized,
):
    path = Path(__file__).resolve().parents[2] / script
    spec = importlib.util.spec_from_file_location("standalone_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first, second = FloatData(value=4.2), FloatData(value=7.5)
    output = StringData(value="scientific output")
    entry = NodeRegistry(name="export_probe", status="completed", arg_hash="input-hash",
        function_hash="", func_mapping="fixture.export_probe", parameters=Parameters(), input_references=[
            NamedDataReference(variable_mapping="simstack.models.base_types.FloatData",
                reference=second.id, variable_name="second"),
            NamedDataReference(variable_mapping="simstack.models.base_types.FloatData",
                reference=first.id, variable_name="first"),
        ], results_references=[NamedDataReference(variable_mapping="simstack.models.base_types.StringData",
            reference=output.id, variable_name="result")])
    task_dir = tmp_path / "work" / entry.name / str(entry.id)
    (task_dir / "nested").mkdir(parents=True)
    (task_dir / "nested" / "result.dat").write_bytes(b"scientific bytes")
    (task_dir / "stdout.txt").write_text("child output")
    values = SimpleNamespace(resolve_task_inputs=AsyncMock(return_value=[second, first]),
                             resolve_task_outputs=AsyncMock(return_value=[output]))
    tasks = SimpleNamespace(get_task=AsyncMock(return_value=entry))
    initialize = AsyncMock()
    monkeypatch.setattr(module, "context", SimpleNamespace(
        initialized=initialized, initialize=initialize,
        config=SimpleNamespace(workdir=tmp_path / "work"),
        server=SimpleNamespace(tasks=tasks, values=values),
    ))

    await module.extract_node_data(str(entry.id), tmp_path / "export")

    target = tmp_path / "export"
    if include_node_name:
        target /= entry.name
    target /= entry.arg_hash
    inputs = json.loads((target / "inputs.json").read_text())
    assert [(item["name"], item["data"]["value"]) for item in inputs] == [
        ("second", 7.5), ("first", 4.2),
    ]
    results = json.loads((target / "results.json").read_text())
    assert results[0]["name"] == "result"
    assert results[0]["data"]["value"] == "scientific output"
    assert json.loads((target / f"{entry.id}.json").read_text())["id"] == str(entry.id)
    assert (target / "nested" / "result.dat").read_bytes() == b"scientific bytes"
    assert (target / "stdout.txt").read_text() == "child output"
    tasks.get_task.assert_awaited_once_with(entry.id)
    values.resolve_task_inputs.assert_awaited_once_with(entry.id)
    values.resolve_task_outputs.assert_awaited_once_with(entry)
    assert initialize.await_count == (0 if initialized else 1)


@pytest.mark.parametrize("script", ["scripts/generate_tests.py", "tests/extract_node_data.py"])
@pytest.mark.parametrize("status", [404, 503])
@pytest.mark.asyncio
async def test_standalone_exports_distinguish_missing_task_from_api_outage(
    monkeypatch, tmp_path, capsys, script, status,
):
    path = Path(__file__).resolve().parents[2] / script
    spec = importlib.util.spec_from_file_location("standalone_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    error = RunnerAPIError("task read failed", status_code=status)
    tasks = SimpleNamespace(get_task=AsyncMock(side_effect=error))
    monkeypatch.setattr(module, "context", SimpleNamespace(
        initialized=True, server=SimpleNamespace(tasks=tasks),
    ))
    identity = str(ObjectId())
    if status == 404:
        await module.extract_node_data(identity, tmp_path / "export")
        assert f"NodeRegistry with ID {identity} not found" in capsys.readouterr().out
    else:
        with pytest.raises(RunnerAPIError) as caught:
            await module.extract_node_data(identity, tmp_path / "export")
        assert caught.value is error
    assert not (tmp_path / "export").exists()
