import base64
import zlib
from typing import Any
from pydantic import field_serializer


class BytesB64Mixin:
    """
    Mixin that teaches Pydantic/ODMantic to serialise *bytes* fields as
    base-64-encoded ASCII strings when exporting to JSON (dict / response).
    """

    @field_serializer("*", mode="wrap", check_fields=False)
    def serialize_bytes(self, value: Any, nxt: Any) -> Any:
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        return nxt(value)

    def _compress_bytes(self, data: bytes) -> str:
            """Compress bytes and encode to base64 string"""
            compressed = zlib.compress(data)
            return base64.b64encode(compressed).decode('utf-8')

    def _decompress_bytes(self, data: str) -> bytes:
            """Decode base64 string and decompress to bytes"""
            compressed = base64.b64decode(data.encode('utf-8'))
            return zlib.decompress(compressed)
