from typing import List, Optional, Dict, Any

from odmantic import Model, Field, EmbeddedModel, ObjectId


class AGGridColumnDef(EmbeddedModel):
    """AG-Grid column definition with support for nested tables."""

    # Core properties
    field: str = Field(..., description="The field name in the row data to bind to")
    headerName: str = Field(..., description="Display name for the column header")

    # Sizing properties
    width: Optional[int] = Field(default=None, description="Column width in pixels")
    minWidth: Optional[int] = Field(
        default=None, description="Minimum column width in pixels"
    )
    maxWidth: Optional[int] = Field(
        default=None, description="Maximum column width in pixels"
    )
    flex: Optional[int] = Field(
        default=None, description="Flex sizing for responsive columns"
    )

    # Display and interaction properties
    hide: Optional[bool] = Field(default=None, description="Whether to hide the column")
    # pinned: Optional[Literal["left", "right"]] = Field(default=None, description="Pin column to left or right")
    sortable: Optional[bool] = Field(
        default=None, description="Whether the column can be sorted"
    )
    resizable: Optional[bool] = Field(
        default=None, description="Whether the column can be resized"
    )
    editable: Optional[bool] = Field(
        default=None, description="Whether cells in this column are editable"
    )

    # # Filtering and rendering
    # filter: Optional[Union[bool, str]] = Field(default=None, description="Filter type")
    # cellRenderer: Optional[str] = Field(default=None, description="Custom cell renderer component")
    # cellRendererParams: Optional[Dict[str, Any]] = Field(default=None, description="Parameters for cell renderer")

    # # Master-Detail specific properties for nested tables
    # cellRendererSelector: Optional[Dict[str, Any]] = Field(default=None, description="Function to select cell renderer based on data")
    #
    # # Nested table configuration
    # is_nested_table: Optional[bool] = Field(default=False, description="Whether this column contains nested table data")
    # nested_table_config: Optional[Dict[str, Any]] = Field(default=None, description="Configuration for nested table rendering")
    #
    # # Row grouping and detail rows
    # rowGroup: Optional[bool] = Field(default=None, description="Whether this column is used for row grouping")
    # showRowGroup: Optional[Union[bool, str]] = Field(default=None, description="Show row group column")
    #
    # # Additional properties
    # custom_properties: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional custom properties")


class TableArtifactModel(Model):
    parent_id: ObjectId = Field(default=None, description="ID of the node registry")
    columns_defs: List[AGGridColumnDef] = Field(
        default_factory=list, description="AG-Grid column definitions"
    )
    row_data: List[Dict[str, Any]] = Field(
        default_factory=list, description="List of row objects"
    )

    # Master-Detail configuration
    master_detail: Optional[bool] = Field(
        default=False, description="Enable master-detail rows"
    )
    detail_cell_renderer: Optional[str] = Field(
        default=None, description="Detail panel cell renderer"
    )
    detail_cell_renderer_params: Optional[Dict[str, Any]] = Field(
        default=None, description="Parameters for detail renderer"
    )
    # TODO implement nested tables and rendering
    # # Nested tables storage - simplified to avoid recursion
    # nested_tables: Optional[Dict[str, str]] = Field(default_factory=dict, description="Nested table IDs by reference key")
    #
    # # Tree data configuration
    # tree_data: Optional[bool] = Field(default=False, description="Enable tree data structure")
    # get_data_path: Optional[str] = Field(default=None, description="Function to get data path for tree structure")
    #
    # # Row grouping
    # auto_group_column_def: Optional[AGGridColumnDef] = Field(default=None, description="Auto group column definition")
    # group_default_expanded: Optional[int] = Field(default=None, description="Default expansion level for groups")
