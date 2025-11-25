from collections import deque
from typing import List, Dict


def find_minimal_route(
    routes: List[Dict[str, str]], source: str, target: str
) -> List[Dict[str, str]]:
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
    if not routes:
        return []

    # Build the graph and route map
    adj: Dict[str, List[str]] = {}
    route_map: Dict[tuple, Dict[str, str]] = {}
    nodes = set()

    for route in routes:
        src = route["source"]
        dst = route["target"]
        nodes.add(src)
        nodes.add(dst)

        if src not in adj:
            adj[src] = []

        # Avoid adding duplicates to adjacency list, but allow route_map update (last one wins)
        if dst not in adj[src]:
            adj[src].append(dst)

        route_map[(src, dst)] = route

    if source not in nodes or target not in nodes:
        return []

    # BFS to find shortest path
    queue = deque([(source, [source])])
    visited = {source}

    while queue:
        current_node, path = queue.popleft()

        if current_node == target:
            # Convert the path of nodes to a list of route dictionaries
            path_routes = []
            for i in range(len(path) - 1):
                u = path[i]
                v = path[i + 1]
                path_routes.append(route_map[(u, v)])
            return path_routes

        if current_node in adj:
            for neighbor in adj[current_node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

    return []


# Add an alias for find_minimal_route as find_shortest_route for compatibility
find_shortest_route = find_minimal_route
