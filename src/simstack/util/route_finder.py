# TODO rmeove duplicate route finder this is a test implementation
routes_table = [
    {"target": "local", "source": "int-nano", "host": "local"},
    {"target": "int-nano", "source": "local", "host": "local"},
    {
        "target": "horeka",
        "source": "local",
        "host": "horeka",
    },
    {
        "target": "local",
        "source": "horeka",
        "host": "horeka",
    },
]


def find_route(target, source):
    """
    Find the minimal route from source to target using available routes.

    Args:
        target (str): The target system
        source (str): The source system

    Returns:
        list: A list of systems to visit in order, or None if no route exists
    """
    # If source and target are the same, no transfer needed
    if source == target:
        return [source]

    # Build a graph of available routes
    graph = {}
    for route in routes_table:
        if route["source"] not in graph:
            graph[route["source"]] = []
        graph[route["source"]].append(route["target"])

    # If source isn't in our graph, we can't start a route
    if source not in graph:
        return None

    # Use BFS to find the shortest path
    visited = {source}
    queue = [(source, [source])]  # (current_node, path_so_far)

    while queue:
        current, path = queue.pop(0)

        # Check neighbors (available transfer targets)
        if current in graph:
            for neighbor in graph[current]:
                if neighbor == target:
                    # Found a path to target
                    return path + [neighbor]

                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

    # If we've exhausted all possibilities without finding a path
    return None


if __name__ == "__main__":
    # Example usage
    target = "int-nano"
    source = "horeka"
    route = find_route(target, source)
    if route:
        print(f"Route from {source} to {target}: {' -> '.join(route)}")
    else:
        print(f"No route found from {source} to {target}")
