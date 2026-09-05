import logging
from types import SimpleNamespace

from simstack.core.find_simstack_modules import find_simstack_modules


def test_find_simstack_modules_lists_entry_points(monkeypatch, caplog):
    fake_entry_points = [
        SimpleNamespace(name="simstack_models", value="simstack.models"),
        SimpleNamespace(name="simstack_nodes", value="simstack.methods"),
    ]
    walked = []

    monkeypatch.setattr(
        "simstack.core.find_simstack_modules.entry_points",
        lambda group: fake_entry_points,
    )
    monkeypatch.setattr(
        "simstack.core.find_simstack_modules.walk_packages",
        lambda package_name, all_modules: walked.append(package_name),
    )

    with caplog.at_level(logging.WARNING, logger="find_modules"):
        find_simstack_modules()

    assert walked == ["simstack.models", "simstack.methods"]
    assert "simstack.modules entry points:" in caplog.text
    assert "simstack_models = simstack.models" in caplog.text
    assert "simstack_nodes = simstack.methods" in caplog.text


def test_find_simstack_modules_lists_when_none_are_registered(monkeypatch, caplog):
    monkeypatch.setattr(
        "simstack.core.find_simstack_modules.entry_points",
        lambda group: [],
    )

    with caplog.at_level(logging.WARNING, logger="find_modules"):
        modules = find_simstack_modules()

    assert modules == []
    assert "No simstack.modules entry points found." in caplog.text
