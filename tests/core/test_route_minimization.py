from simstack.util.minimal_route_finder import find_minimal_route, find_shortest_route


def test_direct_route():
    """Test finding a direct route between two nodes."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
    ]

    result = find_minimal_route(routes, "A", "B")
    expected = [{"source": "A", "target": "B", "host": "A"}]

    assert result == expected
    assert len(result) == 1


def test_multi_hop_route():
    """Test finding a route that requires multiple hops."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
        {"source": "C", "target": "D", "host": "C"},
    ]

    result = find_minimal_route(routes, "A", "D")
    expected = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
        {"source": "C", "target": "D", "host": "C"},
    ]

    assert result == expected
    assert len(result) == 3


def test_no_path_exists():
    """Test when no path exists between source and target."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "C", "target": "D", "host": "C"},
    ]

    result = find_minimal_route(routes, "A", "D")

    assert result == []


def test_same_source_and_target():
    """Test when source and target are the same."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
    ]

    result = find_minimal_route(routes, "A", "A")

    assert result == []


def test_shortest_path_selection():
    """Test that the shortest path is selected when multiple paths exist."""
    routes = [
        # Direct path (1 hop)
        {"source": "A", "target": "D", "host": "A"},
        # Longer path (3 hops)
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
        {"source": "C", "target": "D", "host": "C"},
    ]

    result = find_minimal_route(routes, "A", "D")
    expected = [{"source": "A", "target": "D", "host": "A"}]

    assert result == expected
    assert len(result) == 1


def test_hub_and_spoke_topology():
    """Test routing in a hub-and-spoke topology where all nodes connect through a central hub."""
    routes = [
        {"source": "local", "target": "server1", "host": "local"},
        {"source": "server1", "target": "local", "host": "server1"},
        {"source": "local", "target": "server2", "host": "local"},
        {"source": "server2", "target": "local", "host": "server2"},
        {"source": "local", "target": "server3", "host": "local"},
        {"source": "server3", "target": "local", "host": "server3"},
    ]

    # Test server1 to server2 (should go through local)
    result = find_minimal_route(routes, "server1", "server2")
    expected = [
        {"source": "server1", "target": "local", "host": "server1"},
        {"source": "local", "target": "server2", "host": "local"},
    ]

    assert result == expected
    assert len(result) == 2


def test_empty_routes_list():
    """Test with an empty routes list."""
    routes = []

    result = find_minimal_route(routes, "A", "B")

    assert result == []


def test_single_route():
    """Test with a single route."""
    routes = [{"source": "A", "target": "B", "host": "A"}]

    # Test valid path
    result = find_minimal_route(routes, "A", "B")
    assert result == [{"source": "A", "target": "B", "host": "A"}]

    # Test invalid path
    result = find_minimal_route(routes, "B", "A")
    assert result == []


def test_bidirectional_routes():
    """Test with bidirectional routes."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "A", "host": "B"},
        {"source": "B", "target": "C", "host": "B"},
        {"source": "C", "target": "B", "host": "C"},
    ]

    # Test A to C
    result = find_minimal_route(routes, "A", "C")
    expected = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
    ]
    assert result == expected

    # Test C to A (reverse direction)
    result = find_minimal_route(routes, "C", "A")
    expected = [
        {"source": "C", "target": "B", "host": "C"},
        {"source": "B", "target": "A", "host": "B"},
    ]
    assert result == expected


def test_complex_network():
    """Test with a more complex network topology."""
    routes = [
        {"source": "local", "target": "int-nano", "host": "local"},
        {"source": "int-nano", "target": "local", "host": "local"},
        {"source": "horeka", "target": "local", "host": "horeka"},
        {"source": "local", "target": "horeka", "host": "horeka"},
        {"source": "justus", "target": "local", "host": "justus"},
        {"source": "local", "target": "justus", "host": "justus"},
        {
            "source": "int-nano",
            "target": "horeka",
            "host": "int-nano",
        },  # Direct connection
    ]

    # Test int-nano to horeka (should use direct route)
    result = find_minimal_route(routes, "int-nano", "horeka")
    expected = [{"source": "int-nano", "target": "horeka", "host": "int-nano"}]
    assert result == expected

    # Test horeka to justus (should go through local)
    result = find_minimal_route(routes, "horeka", "justus")
    expected = [
        {"source": "horeka", "target": "local", "host": "horeka"},
        {"source": "local", "target": "justus", "host": "justus"},
    ]
    assert result == expected


def test_nonexistent_nodes():
    """Test with source or target nodes that don't exist in the routes."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
    ]

    # Source doesn't exist
    result = find_minimal_route(routes, "X", "B")
    assert result == []

    # Target doesn't exist
    result = find_minimal_route(routes, "A", "X")
    assert result == []

    # Both don't exist
    result = find_minimal_route(routes, "X", "Y")
    assert result == []


def test_circular_routes():
    """Test with circular routes."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
        {"source": "C", "target": "A", "host": "C"},
    ]

    result = find_minimal_route(routes, "A", "C")
    expected = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
    ]
    assert result == expected


def test_find_shortest_route_alias():
    """Test that find_shortest_route is an alias for find_minimal_route."""
    routes = [
        {"source": "A", "target": "B", "host": "A"},
        {"source": "B", "target": "C", "host": "B"},
    ]

    result1 = find_minimal_route(routes, "A", "C")
    result2 = find_shortest_route(routes, "A", "C")

    assert result1 == result2
    assert find_shortest_route is find_minimal_route


def test_route_data_preservation():
    """Test that all route data (including additional fields) is preserved."""
    routes = [
        {
            "source": "A",
            "target": "B",
            "host": "A",
            "extra_field": "value1",
            "cost": 10,
        },
        {
            "source": "B",
            "target": "C",
            "host": "B",
            "extra_field": "value2",
            "cost": 20,
        },
    ]

    result = find_minimal_route(routes, "A", "C")

    assert len(result) == 2
    assert result[0]["extra_field"] == "value1"
    assert result[0]["cost"] == 10
    assert result[1]["extra_field"] == "value2"
    assert result[1]["cost"] == 20


def test_multiple_routes_same_endpoints():
    """Test behavior when multiple routes exist between the same endpoints."""
    routes = [
        {"source": "A", "target": "B", "host": "A", "type": "fast"},
        {"source": "A", "target": "B", "host": "A", "type": "slow"},  # Duplicate edge
    ]

    # NetworkX will only keep one edge between the same nodes
    result = find_minimal_route(routes, "A", "B")

    assert len(result) == 1
    assert result[0]["source"] == "A"
    assert result[0]["target"] == "B"
    # The last route added should be kept due to how NetworkX handles duplicate edges
    assert result[0]["type"] == "slow"
