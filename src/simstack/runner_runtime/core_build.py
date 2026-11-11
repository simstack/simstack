"""Identify the Core code and execution protocol loaded by a Python runtime."""

from functools import lru_cache
import hashlib
from pathlib import Path

PROTOCOL_VERSION = 3


@lru_cache(maxsize=1)
def core_build_identity() -> str:
    """Include source bytes so editable builds cannot impersonate an old wheel."""
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(str(PROTOCOL_VERSION).encode())
    sources = [*package.rglob("*.py"), package / "static" / "slurm_checkpoint.sh"]
    for source in sorted(sources):
        digest.update(source.relative_to(package).as_posix().encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
