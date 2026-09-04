from types import SimpleNamespace

import pytest

from simstack.models import ArtifactModel
from simstack.models.charts_artifact import (
    ChartArtifactModel,
    make_multi_line_chart,
)
from simstack.util.safe_code_executor import safe_code_executor


def _artifacts():
    return [
        ArtifactModel(
            name="Series A / unsafe",
            data={"plot_data": [{"x": 2, "y": 20}, {"x": 1, "y": 10}]},
        ),
        ArtifactModel(
            name="Series B",
            data={"plot_data": [{"x": 1, "y": 15}, {"x": 3, "y": 30}]},
        ),
    ]


def test_make_multi_line_chart_is_installed_core_function_with_safe_series_keys():
    chart = make_multi_line_chart(_artifacts(), x_key="x", y_key="y")

    assert make_multi_line_chart.__module__ == "simstack.models.charts_artifact"
    assert [series.yKey for series in chart.series] == [
        "Series_A___unsafe__0",
        "Series_B__1",
    ]
    assert chart.data == [
        {"x": 1.0, "Series_A___unsafe__0": 10.0, "Series_B__1": 15.0},
        {"x": 2.0, "Series_A___unsafe__0": 20.0},
        {"x": 3.0, "Series_B__1": 30.0},
    ]


def test_make_multi_line_chart_matches_legacy_empty_behavior():
    chart = make_multi_line_chart([], x_key="x", y_key="y", chart_title="Empty")

    assert chart.data == []
    assert chart.series == []
    assert chart.title.text == "Empty"
    assert chart.axes[0].min is None
    assert chart.axes[0].max is None


def test_safe_code_executor_exposes_packaged_chart_function():
    code = """
def build_chart(arg):
    return make_multi_line_chart(arg.child_artifacts, x_key="x", y_key="y")
"""
    artifact_arguments = SimpleNamespace(child_artifacts=_artifacts())

    result = safe_code_executor(code, artifact_arguments)

    assert result["success"] is True
    assert isinstance(result["result"], ChartArtifactModel)
    assert result["result"].series[0].yKey == "Series_A___unsafe__0"


def test_make_multi_line_chart_requires_axis_keys():
    with pytest.raises(ValueError, match="x_key and y_key"):
        make_multi_line_chart(_artifacts())
