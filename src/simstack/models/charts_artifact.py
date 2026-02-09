from typing import List, Union, Literal, Optional, Dict, Any

from odmantic import Model, Field, EmbeddedModel, ObjectId


# Enterprise-specific series and features
class AGWaterfallSeriesConfig(EmbeddedModel):
    """AG-Charts waterfall series configuration (Enterprise)."""

    type: Literal["waterfall"] = "waterfall"
    xKey: str = Field(..., description="Key for x-axis data")
    yKey: str = Field(..., description="Key for y-axis data")
    labelKey: Optional[str] = Field(None, description="Key for data labels")
    positiveStroke: Optional[str] = Field(None, description="Stroke color for positive values")
    negativeStroke: Optional[str] = Field(None, description="Stroke color for negative values")
    visible: Optional[bool] = Field(True)


class AGHeatmapSeriesConfig(EmbeddedModel):
    """AG-Charts heatmap series configuration (Enterprise)."""

    type: Literal["heatmap"] = "heatmap"
    xKey: str = Field(..., description="Key for x-axis data")
    yKey: str = Field(..., description="Key for y-axis data")
    colorKey: str = Field(..., description="Key for color data")
    visible: Optional[bool] = Field(True)


class AGTreemapSeriesConfig(EmbeddedModel):
    """AG-Charts treemap series configuration (Enterprise)."""

    type: Literal["treemap"] = "treemap"
    labelKey: str = Field(..., description="Key for labels")
    sizeKey: Optional[str] = Field(None, description="Key for size")
    colorKey: Optional[str] = Field(None, description="Key for color")
    visible: Optional[bool] = Field(True)


class AGSunburstSeriesConfig(EmbeddedModel):
    """AG-Charts sunburst series configuration (Enterprise)."""

    type: Literal["sunburst"] = "sunburst"
    labelKey: str = Field(..., description="Key for labels")
    sizeKey: Optional[str] = Field(None, description="Key for size")
    visible: Optional[bool] = Field(True)


# Chart Series Definitions
class AGChartSeriesBase(EmbeddedModel):
    """Base class for AG-Charts series configuration."""

    type: str = Field(..., description="Chart series type")
    xKey: str = Field(..., description="Key for x-axis data")
    yKey: str = Field(..., description="Key for y-axis data")
    visible: Optional[bool] = Field(
        default=True, description="Whether series is visible"
    )
    showInLegend: Optional[bool] = Field(default=True, description="Show in legend")
    title: Optional[str] = Field(default=None, description="Series title")
    # Chart data
    data: List[Dict[str, Any]] = Field(default_factory=list, description="Chart data")


class AGLineSeriesConfig(AGChartSeriesBase):
    """AG-Charts line series configuration."""

    type: Literal["line"] = "line"
    strokeWidth: Optional[float] = Field(default=2, description="Line stroke width")
    strokeOpacity: Optional[float] = Field(default=1, description="Line stroke opacity")
    lineDash: Optional[List[float]] = Field(
        default=None, description="Line dash pattern"
    )
    marker: Optional[Dict[str, Any]] = Field(
        default=None, description="Marker configuration"
    )
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGBarSeriesConfig(AGChartSeriesBase):
    """AG-Charts bar series configuration."""

    type: Literal["bar"] = "bar"
    fillOpacity: Optional[float] = Field(default=1, description="Bar fill opacity")
    strokeWidth: Optional[float] = Field(default=0, description="Bar stroke width")
    cornerRadius: Optional[float] = Field(default=0, description="Bar corner radius")
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGColumnSeriesConfig(AGChartSeriesBase):
    """AG-Charts column series configuration."""

    type: Literal["column"] = "column"
    fillOpacity: Optional[float] = Field(default=1, description="Column fill opacity")
    strokeWidth: Optional[float] = Field(default=0, description="Column stroke width")
    cornerRadius: Optional[float] = Field(default=0, description="Column corner radius")
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGAreaSeriesConfig(AGChartSeriesBase):
    """AG-Charts area series configuration."""

    type: Literal["area"] = "area"
    fillOpacity: Optional[float] = Field(default=0.8, description="Area fill opacity")
    strokeWidth: Optional[float] = Field(default=2, description="Area stroke width")
    marker: Optional[Dict[str, Any]] = Field(
        default=None, description="Marker configuration"
    )
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGScatterSeriesConfig(AGChartSeriesBase):
    """AG-Charts scatter series configuration."""

    type: Literal["scatter"] = "scatter"
    marker: Optional[Dict[str, Any]] = Field(
        default=None, description="Marker configuration"
    )
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGPieSeriesConfig(EmbeddedModel):
    """AG-Charts pie series configuration."""

    type: Literal["pie"] = "pie"
    angleKey: str = Field(..., description="Key for pie slice angles")
    radiusKey: Optional[str] = Field(
        default=None, description="Key for pie slice radius"
    )
    labelKey: Optional[str] = Field(
        default=None, description="Key for pie slice labels"
    )
    visible: Optional[bool] = Field(
        default=True, description="Whether series is visible"
    )
    showInLegend: Optional[bool] = Field(default=True, description="Show in legend")
    title: Optional[str] = Field(default=None, description="Series title")
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


class AGDonutSeriesConfig(EmbeddedModel):
    """AG-Charts donut series configuration."""

    type: Literal["donut"] = "donut"
    angleKey: str = Field(..., description="Key for donut slice angles")
    radiusKey: Optional[str] = Field(
        default=None, description="Key for donut slice radius"
    )
    labelKey: Optional[str] = Field(
        default=None, description="Key for donut slice labels"
    )
    innerRadiusRatio: Optional[float] = Field(
        default=0.6, description="Inner radius ratio"
    )
    visible: Optional[bool] = Field(
        default=True, description="Whether series is visible"
    )
    showInLegend: Optional[bool] = Field(default=True, description="Show in legend")
    title: Optional[str] = Field(default=None, description="Series title")
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Tooltip configuration"
    )


# Union type for all series configurations
AGChartSeries = Union[
    AGLineSeriesConfig,
    AGBarSeriesConfig,
    AGColumnSeriesConfig,
    AGAreaSeriesConfig,
    AGScatterSeriesConfig,
    AGPieSeriesConfig,
    AGDonutSeriesConfig,
    AGWaterfallSeriesConfig,
    AGHeatmapSeriesConfig,
    AGTreemapSeriesConfig,
    AGSunburstSeriesConfig,
]


# Axis Configurations
class AGChartAxisConfig(EmbeddedModel):
    """AG-Charts axis configuration."""

    type: Literal["category", "number", "time", "log"] = Field(
        ..., description="Axis type"
    )
    position: Literal["top", "right", "bottom", "left"] = Field(
        ..., description="Axis position"
    )
    title: Optional[str] = Field(default=None, description="Axis title")
    min: Optional[float] = Field(default=None, description="Minimum axis value")
    max: Optional[float] = Field(default=None, description="Maximum axis value")
    tick: Optional[Dict[str, Any]] = Field(
        default=None, description="Tick configuration"
    )
    label: Optional[Dict[str, Any]] = Field(
        default=None, description="Label configuration"
    )
    gridStyle: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Grid line style"
    )


# Legend Configuration
class AGChartLegendConfig(EmbeddedModel):
    """AG-Charts legend configuration."""

    enabled: Optional[bool] = Field(default=True, description="Enable legend")
    position: Optional[Literal["top", "right", "bottom", "left"]] = Field(
        default="right", description="Legend position"
    )
    spacing: Optional[float] = Field(default=20, description="Legend spacing")
    item: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Legend item configuration"
    )


# Title Configuration
class AGChartTitleConfig(EmbeddedModel):
    """AG-Charts title configuration."""

    text: str = Field("Chart Title", description="Title text")
    enabled: Optional[bool] = Field(default=True, description="Enable title")
    fontSize: Optional[int] = Field(default=16, description="Title font size")
    fontWeight: Optional[str] = Field(default="bold", description="Title font weight")
    color: Optional[str] = Field(default=None, description="Title color")


# Subtitle Configuration
class AGChartSubtitleConfig(EmbeddedModel):
    """AG-Charts subtitle configuration."""

    text: str = Field(..., description="Subtitle text")
    enabled: Optional[bool] = Field(default=True, description="Enable subtitle")
    fontSize: Optional[int] = Field(default=12, description="Subtitle font size")
    color: Optional[str] = Field(default=None, description="Subtitle color")


# Main Chart Model
class ChartArtifactModel(Model):
    """AG-Charts configuration model."""

    parent_id: Optional[ObjectId] = Field(default=None, description="ID of the node registry")

    # Chart data
    data: List[Dict[str, Any]] = Field(default_factory=list, description="Chart data")

    # Chart configuration
    title: Optional[AGChartTitleConfig] = Field(
        default=None, description="Chart title configuration"
    )
    subtitle: Optional[AGChartSubtitleConfig] = Field(
        default=None, description="Chart subtitle configuration"
    )

    # Series configuration
    series: List[AGChartSeries] = Field(
        default_factory=list, description="Chart series configurations"
    )

    # Axes configuration
    axes: List[AGChartAxisConfig] = Field(
        default_factory=list, description="Chart axes configurations"
    )

    # Legend configuration
    legend: Optional[AGChartLegendConfig] = Field(
        default=None, description="Legend configuration"
    )

    # Chart styling and behavior
    width: Optional[int] = Field(default=800, description="Chart width in pixels")
    height: Optional[int] = Field(default=400, description="Chart height in pixels")
    padding: Optional[Dict[str, int]] = Field(default=None, description="Chart padding")
    background: Optional[Dict[str, Any]] = Field(
        default=None, description="Background configuration"
    )

    # Animation
    animation: Optional[Dict[str, Any]] = Field(
        default=None, description="Animation configuration"
    )

    # Tooltip
    tooltip: Optional[Dict[str, Any]] = Field(
        default=None, description="Global tooltip configuration"
    )

    # Theme
    theme: Optional[str] = Field(default="ag-default", description="Chart theme")

    # Additional options
    options: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Additional chart options"
    )


class ChartBuilder:
    """Chainable API for building ChartArtifactModel."""

    def __init__(self, data: Optional[List[Dict[str, Any]]] = None):
        self._chart = ChartArtifactModel(
            data=data or [],
            title=AGChartTitleConfig(text="Chart Title"),
        )

    def with_data(self, data: List[Dict[str, Any]]) -> "ChartBuilder":
        self._chart.data = data
        return self

    def with_title(self, text: str, **kwargs) -> "ChartBuilder":
        if self._chart.title:
            self._chart.title.text = text
            for k, v in kwargs.items():
                setattr(self._chart.title, k, v)
        else:
            self._chart.title = AGChartTitleConfig(text=text, **kwargs)
        return self

    def with_subtitle(self, text: str, **kwargs) -> "ChartBuilder":
        self._chart.subtitle = AGChartSubtitleConfig(text=text, **kwargs)
        return self

    def add_series(self, series: AGChartSeries) -> "ChartBuilder":
        self._chart.series.append(series)
        return self

    def add_line_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGLineSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_bar_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGBarSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_column_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGColumnSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_area_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGAreaSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_scatter_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGScatterSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_pie_series(self, angleKey: str, labelKey: Optional[str] = None, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGPieSeriesConfig(angleKey=angleKey, labelKey=labelKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_donut_series(self, angleKey: str, labelKey: Optional[str] = None, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGDonutSeriesConfig(angleKey=angleKey, labelKey=labelKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_waterfall_series(self, xKey: str, yKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGWaterfallSeriesConfig(xKey=xKey, yKey=yKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_heatmap_series(self, xKey: str, yKey: str, colorKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGHeatmapSeriesConfig(xKey=xKey, yKey=yKey, colorKey=colorKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_treemap_series(self, labelKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGTreemapSeriesConfig(labelKey=labelKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_sunburst_series(self, labelKey: str, title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        series = AGSunburstSeriesConfig(labelKey=labelKey, title=title, **kwargs)
        self._chart.series.append(series)
        return self

    def add_axis(self, type: Literal["category", "number", "time", "log"], position: Literal["top", "right", "bottom", "left"], title: Optional[str] = None, **kwargs) -> "ChartBuilder":
        axis = AGChartAxisConfig(type=type, position=position, title=title, **kwargs)
        self._chart.axes.append(axis)
        return self

    def with_legend(self, enabled: bool = True, position: Literal["top", "right", "bottom", "left"] = "right", **kwargs) -> "ChartBuilder":
        self._chart.legend = AGChartLegendConfig(enabled=enabled, position=position, **kwargs)
        return self

    def with_size(self, width: int, height: int) -> "ChartBuilder":
        self._chart.width = width
        self._chart.height = height
        return self

    def with_theme(self, theme: str) -> "ChartBuilder":
        self._chart.theme = theme
        return self

    def with_options(self, **kwargs) -> "ChartBuilder":
        if self._chart.options is None:
            self._chart.options = {}
        self._chart.options.update(kwargs)
        return self

    def build(self) -> ChartArtifactModel:
        return self._chart


# Helper functions for creating specific chart types
def create_simple_line_chart(
    data: List[Dict[str, Any]],
    x_key: str,
    y_key: str,
    title: Optional[str] = None,
    parent_id: ObjectId = None,
) -> ChartArtifactModel:
    """Create a simple line chart."""
    chart_title = (
        AGChartTitleConfig(text=title) if title else AGChartTitleConfig(text="Chart")
    )
    # subtitle = AGChartSubtitleConfig(text="Subtitle")

    series = [
        AGLineSeriesConfig(
            type="line", xKey=x_key, yKey=y_key, title=y_key.title(), data=data
        )
    ]

    axes = [
        AGChartAxisConfig(type="number", position="bottom", title=x_key.title()),
        AGChartAxisConfig(type="number", position="left", title=y_key.title()),
    ]

    return ChartArtifactModel(
        parent_id=parent_id,
        data=data,
        title=chart_title,
        # subtitle=subtitle,
        series=series,
        axes=axes,
    )


def create_simple_bar_chart(
    data: List[Dict[str, Any]],
    x_key: str,
    y_key: str,
    title: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
) -> ChartArtifactModel:
    """Create a simple bar chart."""
    chart_title = AGChartTitleConfig(text=title) if title else None

    series = [
        AGColumnSeriesConfig(type="column", xKey=x_key, yKey=y_key, title=y_key.title())
    ]

    axes = [
        AGChartAxisConfig(type="category", position="bottom", title=x_key.title()),
        AGChartAxisConfig(type="number", position="left", title=y_key.title()),
    ]

    return ChartArtifactModel(
        parent_id=parent_id, data=data, title=chart_title, series=series, axes=axes
    )


def create_simple_pie_chart(
    data: List[Dict[str, Any]],
    angle_key: str,
    label_key: str,
    title: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
) -> ChartArtifactModel:
    """Create a simple pie chart."""
    chart_title = AGChartTitleConfig(text=title) if title else None

    series = [
        AGPieSeriesConfig(
            type="pie", angleKey=angle_key, labelKey=label_key, title="Distribution"
        )
    ]

    return ChartArtifactModel(
        parent_id=parent_id, data=data, title=chart_title, series=series
    )


def create_simple_area_chart(
    data: List[Dict[str, Any]],
    x_key: str,
    y_key: str,
    title: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
) -> ChartArtifactModel:
    """Create a simple area chart."""
    chart_title = AGChartTitleConfig(text=title) if title else None

    series = [
        AGAreaSeriesConfig(type="area", xKey=x_key, yKey=y_key, title=y_key.title())
    ]

    axes = [
        AGChartAxisConfig(type="category", position="bottom", title=x_key.title()),
        AGChartAxisConfig(type="number", position="left", title=y_key.title()),
    ]

    return ChartArtifactModel(
        parent_id=parent_id, data=data, title=chart_title, series=series, axes=axes
    )


def create_simple_scatter_chart(
    data: List[Dict[str, Any]],
    x_key: str,
    y_key: str,
    title: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
) -> ChartArtifactModel:
    """Create a simple scatter chart."""
    chart_title = AGChartTitleConfig(text=title) if title else None

    series = [
        AGScatterSeriesConfig(
            type="scatter", xKey=x_key, yKey=y_key, title=y_key.title()
        )
    ]

    axes = [
        AGChartAxisConfig(type="number", position="bottom", title=x_key.title()),
        AGChartAxisConfig(type="number", position="left", title=y_key.title()),
    ]

    return ChartArtifactModel(
        parent_id=parent_id, data=data, title=chart_title, series=series, axes=axes
    )


def create_simple_donut_chart(
    data: List[Dict[str, Any]],
    angle_key: str,
    label_key: str,
    title: Optional[str] = None,
    inner_radius_ratio: float = 0.6,
    parent_id: Optional[ObjectId] = None,
) -> ChartArtifactModel:
    """Create a simple donut chart."""
    chart_title = AGChartTitleConfig(text=title) if title else None

    series = [
        AGDonutSeriesConfig(
            type="donut",
            angleKey=angle_key,
            labelKey=label_key,
            innerRadiusRatio=inner_radius_ratio,
            title="Distribution",
        )
    ]

    return ChartArtifactModel(
        parent_id=parent_id, data=data, title=chart_title, series=series
    )
