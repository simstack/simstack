import subprocess

import pytest

from simstack.util.repository_runtime import verify_source


def git(root,*arguments):
    return subprocess.check_output(["git","-C",str(root),*arguments],text=True).strip()


def init(root):
    root.mkdir()
    git(root,"init","--quiet")
    git(root,"config","user.name","Fixture")
    git(root,"config","user.email","fixture@example.invalid")


def test_recursive_submodule_pin_and_dirty_sources_are_checked(tmp_path):
    child = tmp_path/"child"
    init(child)
    (child/"code.py").write_text("VALUE=1\n")
    git(child,"add","code.py");git(child,"commit","--quiet","-m","Initial")
    child_sha = git(child,"rev-parse","HEAD")
    root = tmp_path/"root"
    init(root)
    git(root,"-c","protocol.file.allow=always","submodule","add","--quiet",str(child),"nested lib")
    git(root,"commit","--quiet","-am","Add source")
    descriptor = {"commit_sha":git(root,"rev-parse","HEAD"),
                  "submodules":[{"path":"nested lib","commit_sha":child_sha}]}
    git(root,"tag","v1")
    descriptor["git_metadata"] = [{"path":".","tags":[{"name":"refs/tags/v1","object_sha":descriptor["commit_sha"]}]},
        {"path":"nested lib","tags":[]}]
    verify_source(root,descriptor)
    git(root,"tag","unrecorded-tag")
    with pytest.raises(RuntimeError,match="tags"):
        verify_source(root,descriptor)
    git(root,"tag","-d","unrecorded-tag")
    (root/"nested lib/code.py").write_text("VALUE=2\n")
    with pytest.raises(RuntimeError,match="modified|changed"):
        verify_source(root,descriptor)
    git(root/"nested lib","checkout","--","code.py")
    verify_source(root,descriptor)
    descriptor["submodules"][0]["commit_sha"]="b"*40
    with pytest.raises(RuntimeError,match="differ"):
        verify_source(root,descriptor)
