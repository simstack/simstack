from simstack.util.minimal_route_finder import find_minimal_route


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
    if source == target:
        return [source]
    route = find_minimal_route(routes_table, source, target)
    return [source, *(step["target"] for step in route)] if route else None
