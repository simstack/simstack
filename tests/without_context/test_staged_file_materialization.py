from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import shutil
import threading
from types import SimpleNamespace

from simstack.core.context import context
from simstack.models.file_instance import FileInstance
from simstack.models.files import FileStack
from simstack.models.parameters import Resource


def test_materializing_staged_input_serializes_with_upload_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_config", SimpleNamespace(workdir=tmp_path, resource="local"))
    value = FileStack.from_string("scientific input bytes", "input.txt")
    entered, resume, upload_started = threading.Event(), threading.Event(), threading.Event()
    copyfile = shutil.copyfile

    def delayed_copy(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return copyfile(*args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", delayed_copy)

    class Client:
        object_api_prefix = "/runner/objects"

        def publish(self, file, path):
            assert path.read_text() == "scientific input bytes"
            return FileInstance(path="object", object_key="object", bucket="files",
                location_type="s3", resource="__s3__", created_at=datetime.now()).model_dump()

    def upload():
        upload_started.set()
        value.ensure_uploaded(client=Client())

    with ThreadPoolExecutor(2) as executor:
        copying = executor.submit(value.get_raw, Resource(value="local"), tmp_path / "materialized")
        try:
            assert entered.wait(5)
            uploading = executor.submit(upload)
            assert upload_started.wait(5)
            assert not uploading.done()
        finally:
            resume.set()
        result = copying.result(timeout=5)
        uploading.result(timeout=5)
    assert result.read_text() == "scientific input bytes"
    assert value._staging_directory is None
