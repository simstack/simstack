import threading
from typing import Dict

from simstack.core.artifacts import ArtifactArguments


def safe_code_executor(
    code_string: str, artifact_arguments: ArtifactArguments, timeout: int = 30
) -> Dict:
    """
    Safely executes Python code from a string with a controlled environment.

    Args:
        code_string (str): The Python code to execute
        artifact_arguments (ArtifactArguments): Artifact arguments to be used in the execution environment
        timeout (int, optional): Maximum execution time in seconds before timeout

    Returns:
        Dict: A dictionary containing:
            'success' (bool): Whether execution was successful
            'result': The return value if successful
            'error': Error message if unsuccessful
            'error_type': Type of error if unsuccessful

    """
    import ast
    import traceback

    # Default result structure
    result = {"success": False, "result": None, "error": None, "error_type": None}

    # Verify code is not empty
    if not code_string or not code_string.strip():
        result["error"] = "Empty code string provided"
        result["error_type"] = "ValueError"
        return result

    # Create a safe globals dictionary
    safe_globals = {"__builtins__": {}}

    # Add only whitelisted builtins that are considered safe
    for name in [
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "map",
        "max",
        "min",
        "print",
        "range",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    ]:
        safe_globals["__builtins__"][name] = __builtins__[name]

    # Add artifact models to safe globals
    from simstack.models.charts_artifact import (
        ChartArtifactModel,
        AGLineSeriesConfig,
        AGChartAxisConfig,
        AGChartTitleConfig,
        AGChartLegendConfig,
    )
    from simstack.models.table_artifact import TableArtifactModel
    from examples.science.electronic_structure.spectra.plot_spectra import (
        make_multi_line_chart,
    )
    from simstack.models import ArtifactModel

    safe_globals.update(
        {
            "ChartArtifactModel": ChartArtifactModel,
            "TableArtifactModel": TableArtifactModel,
            "AGLineSeriesConfig": AGLineSeriesConfig,
            "AGChartAxisConfig": AGChartAxisConfig,
            "AGChartTitleConfig": AGChartTitleConfig,
            "AGChartLegendConfig": AGChartLegendConfig,
            "make_multi_line_chart": make_multi_line_chart,
            "ArtifactModel": ArtifactModel,
            "ArtifactArguments": ArtifactArguments,
        }
    )

    # Add artifact_arguments attributes to safe globals
    # Extract all attributes from the artifact_arguments instance
    for attr_name in dir(artifact_arguments):
        if not attr_name.startswith("_"):  # Skip private/magic methods
            attr_value = getattr(artifact_arguments, attr_name)
            if not callable(attr_value):  # Skip methods, only include data attributes
                safe_globals[attr_name] = attr_value

    # Also add the full artifact_arguments object itself for backward compatibility
    safe_globals["arg"] = artifact_arguments

    # Verify code is syntactically correct
    try:
        ast.parse(code_string)
    except SyntaxError as e:
        result["error"] = str(e)
        result["error_type"] = "SyntaxError"
        return result

    # Cross-platform timeout implementation using threading
    execution_result = {
        "completed": False,
        "exception": None,
        "local_vars": {},
        "function_result": None,
    }

    def execute_code():
        try:
            local_vars = {}
            exec(code_string, safe_globals, local_vars)
            execution_result["local_vars"] = local_vars

            # Look for a function in local_vars and call it with artifact_arguments
            function_result = None
            for var_name, var_value in local_vars.items():
                if callable(var_value) and not var_name.startswith("_"):
                    # Found a function, call it with artifact_arguments
                    try:
                        function_result = var_value(artifact_arguments)
                        execution_result["function_result"] = function_result
                        break
                    except Exception as func_e:
                        execution_result["exception"] = func_e
                        execution_result["completed"] = True
                        return

            execution_result["completed"] = True
        except Exception as e:
            execution_result["exception"] = e
            execution_result["completed"] = True

    try:
        # Start execution in a separate thread
        thread = threading.Thread(target=execute_code, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            # Timeout occurred
            result["error"] = f"Code execution timed out after {timeout} seconds"
            result["error_type"] = "TimeoutError"
            return result

        if execution_result["exception"]:
            # Exception occurred during execution
            e = execution_result["exception"]
            result["error"] = str(e)
            result["error_type"] = e.__class__.__name__
            result["traceback"] = traceback.format_exc()
            return result

        # Successful execution
        # Return the function result if available, otherwise return local vars
        if execution_result["function_result"] is not None:
            result["result"] = execution_result["function_result"]
        else:
            local_vars = execution_result["local_vars"]
            if "result" in local_vars:
                result["result"] = local_vars["result"]
            else:
                result["result"] = {
                    k: v for k, v in local_vars.items() if not k.startswith("_")
                }

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = e.__class__.__name__
        result["traceback"] = traceback.format_exc()

    return result
