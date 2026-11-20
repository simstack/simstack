import hashlib
import subprocess
import sys

from simstack.core.hash import complex_hash_function


def test_bytes_hash_is_stable_and_distinct_from_text():
    payload = b"fcc-file-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    assert complex_hash_function(payload) == f"binary-{digest}"
    assert complex_hash_function(bytearray(payload)) == f"binary-{digest}"
    assert complex_hash_function(payload.decode()) == digest
    assert complex_hash_function(b"other") != f"binary-{digest}"


def test_bytes_hash_matches_across_processes():
    script = (
        "from simstack.core.hash import complex_hash_function\n"
        "print(complex_hash_function(b'fcc-file-bytes'))\n"
    )
    first = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    assert first == second == complex_hash_function(b"fcc-file-bytes")
