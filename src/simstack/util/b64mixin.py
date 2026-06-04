from pydantic import PlainSerializer
from typing_extensions import Annotated
import base64
import zlib

B64Bytes = Annotated[
    bytes, PlainSerializer(lambda b: base64.b64encode(b).decode("ascii"), when_used="json")
]

class BytesB64Mixin:
    """
    Mixin that teaches Pydantic/ODMantic to serialise *bytes* fields as
    base-64-encoded ASCII strings when exporting to JSON (dict / response).
    """

    def _compress_bytes(self, data: bytes) -> str:
            """Compress bytes and encode to base64 string"""
            compressed = zlib.compress(data)
            return base64.b64encode(compressed).decode('utf-8')

    def _decompress_bytes(self, data: str) -> bytes:
            """Decode base64 string and decompress to bytes"""
            compressed = base64.b64decode(data.encode('utf-8'))
            return zlib.decompress(compressed)
