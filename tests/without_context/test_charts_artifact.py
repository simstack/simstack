import json

from simstack.models.charts_artifact import (
    AGBarSeriesConfig,
    AGChartTitleConfig,
    AGColumnSeriesConfig,
    ChartArtifactModel,
    create_simple_bar_chart,
)


def test_create_simple_bar_chart_uses_ag_charts_v11_vertical_bar_contract():
    chart = create_simple_bar_chart(
        data=[{"category": "A", "value": 1.0}],
        x_key="category",
        y_key="value",
        title="Values",
    )

    assert isinstance(chart.series[0], AGBarSeriesConfig)
    assert chart.series[0].type == "bar"
    assert chart.series[0].direction == "vertical"

    serialized_series = chart.model_dump(mode="json")["series"][0]
    assert serialized_series["type"] == "bar"
    assert serialized_series["direction"] == "vertical"


def test_legacy_column_chart_document_remains_readable():
    legacy_chart = ChartArtifactModel(
        title=AGChartTitleConfig(text="Legacy column chart"),
        data=[{"category": "A", "value": 1.0}],
        series=[
            AGBarSeriesConfig(
                type="bar",
                direction="vertical",
                xKey="category",
                yKey="value",
            )
        ],
    )
    persisted_document = legacy_chart.model_dump_doc()
    persisted_document["series"][0]["type"] = "column"
    del persisted_document["series"][0]["direction"]

    loaded_chart = ChartArtifactModel.model_validate_doc(persisted_document)

    assert isinstance(loaded_chart.series[0], AGBarSeriesConfig)
    assert loaded_chart.series[0].type == "bar"
    assert loaded_chart.series[0].direction == "vertical"
    assert loaded_chart.series[0].xKey == "category"
    assert loaded_chart.series[0].yKey == "value"


def test_legacy_column_model_instance_is_normalized():
    chart = ChartArtifactModel(
        title=AGChartTitleConfig(text="Legacy model instance"),
        series=[
            AGColumnSeriesConfig(
                xKey="category",
                yKey="value",
                data=[{"category": "A", "value": 1.0}],
            )
        ],
    )

    assert isinstance(chart.series[0], AGBarSeriesConfig)
    assert chart.series[0].type == "bar"
    assert chart.series[0].direction == "vertical"
    assert chart.series[0].data == [{"category": "A", "value": 1.0}]


def test_existing_bar_document_without_direction_remains_unspecified():
    existing_chart = ChartArtifactModel(
        title=AGChartTitleConfig(text="Existing bar chart"),
        data=[{"category": "A", "value": 1.0}],
        series=[AGBarSeriesConfig(xKey="category", yKey="value")],
    )
    persisted_document = existing_chart.model_dump_doc()
    del persisted_document["series"][0]["direction"]

    loaded_chart = ChartArtifactModel.model_validate_doc(persisted_document)

    assert isinstance(loaded_chart.series[0], AGBarSeriesConfig)
    assert loaded_chart.series[0].direction is None


def test_chart_schema_excludes_legacy_column_and_exposes_bar_direction():
    schema = ChartArtifactModel.model_json_schema()
    series_variants = schema["properties"]["series"]["items"]["anyOf"]
    direction_schema = schema["$defs"]["AGBarSeriesConfig"]["properties"][
        "direction"
    ]
    serialized_schema = json.dumps(schema)

    assert {"$ref": "#/$defs/AGColumnSeriesConfig"} not in series_variants
    assert "AGColumnSeriesConfig" not in schema["$defs"]
    assert '"column"' not in serialized_schema
    assert direction_schema["default"] is None
    assert {"enum": ["horizontal", "vertical"], "type": "string"} in (
        direction_schema["anyOf"]
    )
