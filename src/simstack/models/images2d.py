from typing import Optional, Literal, Dict, Any, ClassVar
import base64
import os
from simstack.models.document import Field, Model, ObjectId, Reference
from simstack.models.files import FileStack
from simstack.models.simstack_model import simstack_model

@simstack_model
class Image2DArtifactModel(Model):
    """Document model for a serialized 2D image artifact."""
    __payload_fields__: ClassVar[tuple[dict, ...]] = ({
        "source": "data", "target": "payload_file", "codec": "image", "codec_version": 1,
    },)

    parent_id: Optional[ObjectId] = None
    name: str = Field(..., description="Name of the image artifact")
    description: Optional[str] = Field(None, description="Description of the image")
    format: Literal["png", "jpg", "jpeg", "svg", "gif", "bmp", "webp"] = Field(
        ..., description="Image format"
    )
    inline_data: bytes = Field(default=b"", alias="data", serialization_alias="data",
                               description="Legacy inline binary image data")
    payload_file: Optional[FileStack] = Reference(None)
    width: Optional[int] = Field(None, description="Image width in pixels")
    height: Optional[int] = Field(None, description="Image height in pixels")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    def prepare_blob_payload(self) -> None:
        if self.payload_file is None:
            self.payload_file = FileStack.from_bytes(self.inline_data, f"image.{self.format}")
            self.inline_data = b""

    @property
    def data(self) -> bytes:
        """Preserve image.data reads while persisted documents carry references."""
        return self.get_bytes()

    @data.setter
    def data(self, value: bytes) -> None:
        self.inline_data = value
        self.payload_file = None

    def get_bytes(self) -> bytes:
        return self.payload_file.read_bytes() if self.payload_file is not None else self.inline_data

    def to_base64(self) -> str:
        """Convert binary data to base64 string."""
        return base64.b64encode(self.get_bytes()).decode("utf-8")

    def get_data_uri(self) -> str:
        """Get data URI for the image."""
        mime_type = f"image/{self.format}"
        if self.format == "svg":
            mime_type = "image/svg+xml"
        return f"data:{mime_type};base64,{self.to_base64()}"

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {
            "name": self.name,
            "format": self.format,
            "size": f"{self.width}x{self.height}" if self.width and self.height else "unknown"
        }

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [
            {"field": "name", "headerName": "Name"},
            {"field": "format", "headerName": "Format"},
            {"field": "size", "headerName": "Size"},
        ]

    def make_table_entries_instance(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return self.make_table_entries(
            max_recursion_level=max_recursion_level,
            drop_id=drop_id,
            current_level=current_level,
            visited=visited,
            field_prefix=field_prefix,
        )

    @classmethod
    def ui_schema(cls) -> Dict[str, Any]:
        return {
            "payload_file": {
                "ui:field": "FileField",
                "ui:options": {"isInput": False},
            },
            "ui:table": {
                "pagination": True,
                "paginationPageSize": 20,
                "domLayout": "autoHeight",
                "defaultColDef": {"flex": 1, "minWidth": 80, "resizable": True},
            }
        }

    @classmethod
    def ui_make_title(cls, ui_schema: Dict[str, Any], field: str, title: str) -> dict:
        if "ui:table" not in ui_schema:
            ui_schema = cls.ui_schema()
        return ui_schema

def create_image_artifact(
    name: str,
    data: bytes,
    format: str,
    description: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Image2DArtifactModel:
    """Create an Image2DArtifactModel instance."""
    return Image2DArtifactModel(
        name=name,
        data=data,
        format=format.lower(),
        description=description,
        parent_id=parent_id,
        width=width,
        height=height,
        metadata=metadata or {},
    )

def create_image_artifact_from_file(
    path: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    parent_id: Optional[ObjectId] = None,
) -> Image2DArtifactModel:
    """Create an Image2DArtifactModel instance from a file path."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Image file not found: {path}")

    filename = os.path.basename(path)
    if name is None:
        name = filename

    # Extract format from extension
    _, ext = os.path.splitext(filename)
    format = ext.lstrip(".").lower()
    if not format:
         format = "png" # Default

    with open(path, "rb") as f:
        data = f.read()

    return create_image_artifact(
        name=name,
        data=data,
        format=format,
        description=description,
        parent_id=parent_id
    )
