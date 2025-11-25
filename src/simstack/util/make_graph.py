def model_to_graph_data(model, exclude_none=True, exclude_unset=True, exclude=None):
    """
    Checks if an odmantic Model contains fields that can be interpreted as graph data
    (multiple lists of the same length) and converts it to a format suitable for ag-graph.

    Args:
        model: An odmantic Model instance
        exclude_none (bool): Whether to exclude None values
        exclude_unset (bool): Whether to exclude unset values
        exclude (set): Fields to exclude

    Returns:
        dict: A dictionary with graph data if the model contains compatible fields, otherwise None
    """
    if exclude is None:
        exclude = set()

    # Collect all list fields from the model
    list_fields = {}

    # Get all fields from the model
    if hasattr(model, "__fields__"):
        fields = model.__fields__
    else:
        # Fallback if field access pattern is different
        fields = {
            k: None
            for k in dir(model)
            if not k.startswith("_") and not callable(getattr(model, k))
        }

    # Process each field to find lists with numeric or string values
    for field_name in fields:
        if field_name in exclude:
            continue

        value = getattr(model, field_name, None)

        # Skip None values if exclude_none is True
        if value is None and exclude_none:
            continue

        # Handle unset values if exclude_unset is True
        if exclude_unset and value == getattr(
            fields.get(field_name), "default", object()
        ):
            continue

        # Check if the field is a list with numeric or string values
        if isinstance(value, list) and len(value) > 0:
            # Check if all elements are numeric or strings
            if all(isinstance(item, (int, float, str)) for item in value):
                list_fields[field_name] = value

    # If we don't have at least two lists, we can't make a graph
    if len(list_fields) < 2:
        return None

    # Check if all lists have the same length
    list_lengths = {len(lst) for lst in list_fields.values()}
    if len(list_lengths) != 1:
        return None  # Not all lists have the same length

    list_length = next(iter(list_lengths))
    if list_length == 0:
        return None  # Empty lists

    # Generate data series for each potential y-axis
    series = []

    # Use first string list as labels if available, otherwise use index
    x_axis_field = None
    for field_name, values in list_fields.items():
        if all(isinstance(item, str) for item in values):
            x_axis_field = field_name
            break

    # If no string list found, use the first field as x-axis
    if x_axis_field is None:
        x_axis_field = next(iter(list_fields.keys()))

    x_values = list_fields[x_axis_field]

    # Create series for each numeric list (except the x-axis)
    for field_name, values in list_fields.items():
        if field_name != x_axis_field:
            # Create a series for this y-axis
            series_data = []
            for i in range(list_length):
                series_data.append({"x": x_values[i], "y": values[i]})

            series.append({"name": field_name, "data": series_data})

    # Create the graph data structure
    graph_data = {
        "chart": {
            "type": "line"  # Default chart type, can be changed
        },
        "title": {
            "text": model.__class__.__name__  # Use model class name as title
        },
        "xAxis": {"title": {"text": x_axis_field}},
        "yAxis": {"title": {"text": "Value"}},
        "series": series,
    }

    return graph_data
