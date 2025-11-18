import networkx as nx
from typing import List, Dict, Optional


def find_minimal_route(routes: List[Dict[str, str]], source: str, target: str) -> List[Dict[str, str]]:
    """
    Find the minimal (shortest) route from source to target.
    
    Args:
        routes: List of route dictionaries with 'source', 'target', and 'host'
        source: The starting node
        target: The destination node
        
    Returns:
        A list of route dictionaries representing the shortest path from source to target,
        or an empty list if no path exists
    """
    # Create a directed graph
    G = nx.DiGraph()

    if len(routes) == 0:
        return []
    # Add all edges with associated route data
    route_map = {}  # To map edge (u,v) to its route dict
    for route in routes:
        src = route['source']
        dst = route['target']
        G.add_edge(src, dst)
        route_map[(src, dst)] = route

    if source not in G or target not in G:
        return []

    # Check if a path exists
    if not nx.has_path(G, source, target):
        return []

    try:
        # Find the shortest path from source to target
        path_nodes = nx.shortest_path(G, source, target)

        # Convert the path to a list of route dictionaries
        path_routes = []
        for i in range(len(path_nodes) - 1):
            src = path_nodes[i]
            dst = path_nodes[i + 1]
            path_routes.append(route_map[(src, dst)])

        return path_routes

    except nx.NetworkXNoPath:
        return []  # No path exists
    except Exception as e:
        print(f"Error finding path: {e}")
        return []

# Add an alias for find_minimal_route as find_shortest_route for compatibility
find_shortest_route = find_minimal_route


# Example usage
def test_minimal_route():
    # Example routes
    routes = [
        {'source': 'local', 'target': 'int-nano', 'host': 'local'},
        {'source': 'int-nano', 'target': 'local', 'host': 'local'},
        {'source': 'horeka', 'target': 'local', 'host': 'horeka'},
        {'source': 'local', 'target': 'horeka', 'host': 'horeka'},
        {'source': 'justus', 'target': 'local', 'host': 'justus'},
        {'source': 'local', 'target': 'justus', 'host': 'justus'},
    ]

    # Test cases
    test_cases = [
        ('local', 'horeka'),
        ('local', 'int-nano'),
        ('int-nano', 'horeka'),
        ('int-nano', 'justus'),  # This should go through 'local'
        ('horeka', 'justus'),  # This should go through 'local'
    ]

    for src, dst in test_cases:
        path = find_minimal_route(routes, src, dst)

        print(f"\nPath from {src} to {dst}:")
        if not path:
            print("  No path exists")
        else:
            for i, route in enumerate(path):
                print(f"  Step {i + 1}: {route['source']} -> {route['target']} (host: {route['host']})")