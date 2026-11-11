import os
from pathlib import Path
import subprocess
import sys

import pytest

from simstack.runner_runtime.file_cache import evict_cached_file


def test_cache_eviction_waits_for_scientific_process_exit(tmp_path):
    path = tmp_path / "scientific-input"
    path.write_text("input that must survive cleanup")
    script = """
import sys
from pathlib import Path
from simstack.runner_runtime.file_cache import pin_cached_file
assert pin_cached_file(Path(sys.argv[1]))
print('pinned', flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert process.stdout.readline().strip() == "pinned"
        assert evict_cached_file(path) is False
        assert path.read_text() == "input that must survive cleanup"
        process.kill()
        process.wait(timeout=10)
        assert evict_cached_file(path) is True
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-limit regression")
def test_many_input_pins_do_not_exhaust_process_descriptors(tmp_path):
    script = """
import resource, sys
from pathlib import Path
from simstack.runner_runtime.file_cache import pin_cached_file
root = Path(sys.argv[1])
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(48, hard), hard))
for index in range(256):
    path = root / str(index)
    path.write_text('scientific input')
    assert pin_cached_file(path, workdir=root)
print('pinned', flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert process.stdout.readline().strip() == "pinned"
        assert evict_cached_file(tmp_path / "255") is False
        process.kill()
        process.wait(timeout=10)
        assert evict_cached_file(tmp_path / "255") is True
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_eviction_cannot_pass_a_pin_being_published(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from simstack.runner_runtime import file_cache
    path = tmp_path / "input"
    path.write_text("science")
    entered, resume = threading.Event(), threading.Event()
    original = file_cache._owner

    def delayed_owner(workdir):
        entered.set()
        assert resume.wait(5)
        return original(workdir)

    monkeypatch.setattr(file_cache, "_owner", delayed_owner)
    with ThreadPoolExecutor(1) as executor:
        pinning = executor.submit(file_cache.pin_cached_file, path, workdir=tmp_path)
        try:
            assert entered.wait(5)
            assert evict_cached_file(path) is False
            assert path.read_text() == "science"
        finally:
            resume.set()
        assert pinning.result(timeout=5)
    assert evict_cached_file(path) is False


def test_absent_download_target_is_reserved_until_process_exit(tmp_path):
    path = tmp_path / "replacement-input"
    script = """
import sys
from pathlib import Path
from simstack.runner_runtime.file_cache import pin_cached_file
path = Path(sys.argv[1])
assert pin_cached_file(path, workdir=path.parent, allow_missing=True)
print('reserved', flush=True)
sys.stdin.readline()
path.write_text('replacement scientific input')
print('published', flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert process.stdout.readline().strip() == "reserved"
        assert not path.exists()
        assert evict_cached_file(path) is False
        process.stdin.write("publish\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "published"
        assert evict_cached_file(path) is False
        assert path.read_text() == "replacement scientific input"
        process.kill()
        process.wait(timeout=10)
        assert evict_cached_file(path) is True
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
