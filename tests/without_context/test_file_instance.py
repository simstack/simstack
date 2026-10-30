from types import SimpleNamespace

from odmantic import ObjectId

from simstack.models.file_instance import FileInstance
from simstack.models.parameters import Resource
from simstack.util.file_transfer_client import resolve_instance_path


def test_external_file_instance_resolves_to_copied_file(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    source = tmp_path / "final_geometry.xyz"
    content = b"1\ngeometry\nH 0 0 0\n"
    source.write_bytes(content)
    monkeypatch.setattr(
        "simstack.core.context.context",
        SimpleNamespace(
            config=SimpleNamespace(workdir=workdir, resource=Resource(value="local"))
        ),
    )
    monkeypatch.setattr("getpass.getuser", lambda: "test-user")

    instance = FileInstance.from_local_file(source, ObjectId())
    source.unlink()

    copied_file = resolve_instance_path(instance.path, workdir)
    assert copied_file.is_file()
    assert copied_file.read_bytes() == content
