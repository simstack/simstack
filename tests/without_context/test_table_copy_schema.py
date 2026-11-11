"""Logical table types remain identifiable in schemas sent to the Copy UI."""
from simstack.models.document import Model, Reference
from simstack.models.simple_table import SimpleTable
from simstack.models.table_artifact import TableArtifactModel
from simstack.models.charts_artifact import ChartArtifactModel


def test_table_payload_markers_survive_nested_references_and_nullable_maps():
    class Experiment(Model):
        simple: SimpleTable = Reference()
        tables: list[TableArtifactModel] = Reference()
        charts: dict[str, ChartArtifactModel | None]
        scientific: dict

    schema = Experiment.model_json_schema()
    definitions = schema["$defs"]
    assert definitions["SimpleTable"]["x-simstack-table-payload"] == "simple-table"
    assert definitions["TableArtifactModel"]["x-simstack-table-payload"] == "table"
    assert definitions["ChartArtifactModel"]["x-simstack-table-payload"] == "chart"
    assert "x-simstack-table-payload" not in schema["properties"]["scientific"]
