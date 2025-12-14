import pytest
from src.simstack.util.route_finder import find_route


@pytest.fixture
def sample_routes():
    return [
        {"source": "A", "target": "B"},
        {"source": "B", "target": "C"},
        {"source": "C", "target": "D"},
        {"source": "A", "target": "E"},
    ]


def test_direct_route(sample_routes, monkeypatch):
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", sample_routes)
    assert find_route("B", "A") == ["A", "B"], "Failed to find direct route"


def test_indirect_route(sample_routes, monkeypatch):
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", sample_routes)
    assert find_route("D", "A") == ["A", "B", "C", "D"], "Failed to find indirect route"


def test_no_route_available(sample_routes, monkeypatch):
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", sample_routes)
    assert find_route("Z", "A") is None, "Route should not exist but was found"


def test_source_equals_target(sample_routes, monkeypatch):
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", sample_routes)
    assert find_route("A", "A") == ["A"], "Failed to handle source equals target"


def test_isolated_source(sample_routes, monkeypatch):
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", sample_routes)
    assert find_route("A", "Z") is None, "Isolated source should return no route"


def test_route_with_multiple_options(sample_routes, monkeypatch):
    extended_routes = sample_routes + [{"source": "A", "target": "C"}]
    monkeypatch.setattr("src.simstack.util.route_finder.routes_table", extended_routes)
    assert find_route("D", "A") == [
        "A",
        "C",
        "D",
    ], "Failed to find shortest route with multiple options"
