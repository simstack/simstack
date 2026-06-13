Testing Complex Nodes
=====================

This document explains the testing strategy for complex nodes in SimStack, focusing on the test generation and re-execution mechanism.

Overview
--------

In SimStack, some nodes perform complex tasks that might be difficult to reproduce in a standard unit test environment due to external dependencies, large data volumes, or non-deterministic behavior. To address this, we use a strategy of capturing the state of a successful node execution and using it to generate a repeatable test case.

The two main components of this strategy are:
1. `simstack/src/simstack/methods/generate_test.py`: A tool to extract node data from the database and work directory.
2. `examples/testing/testing_test_generation.py`: An example script demonstrating how to use generated tests and patch complex executions.

Test Generation (`generate_test.py`)
------------------------------------

The `generate_test` method (and its CLI wrapper) allows developers to create a test case from an existing `NodeRegistry` entry.

Workflow:
1. **Identify Node**: The user provides a `node_id` (ObjectId of a `NodeRegistry` entry).
2. **Setup Target**: A target directory is created, organized by node name and argument hash: `target_base / node_name / arg_hash`.
3. **Capture Files**: All files from the node's work directory (`workdir / node_name / node_id`) are copied to the target directory.
4. **Serialize Data**:
   - Input models are loaded from the database based on `input_references` and serialized to `inputs.json`.
   - Output models (results) are loaded based on `results_references` and serialized to `outputs.json`.
   - This is for convenience and not really needed for the test

This results in a self-contained directory containing everything needed to verify the node's behavior for a specific set of inputs.

Testing with Patches (`testing_test_generation.py`)
--------------------------------------------------

Once a test case is generated, it can be used to verify that changes to the node's logic don't break existing functionality, or to debug complex failures.

The example script `testing_test_generation.py` demonstrates a pattern for testing nodes that call external functions or have complex side effects.

### The Problem: Complex Side Effects
Some nodes call functions that perform operations (like submitting a job to a cluster) which cannot be easily duplicated in testing.
The idea is to store all of the resulting inout and output files and also serialized versions of all costly intermediate results.
During a test run, we don't want to actually submit a new job; instead, we want to simulate the previous execution and
return the *original* `task_id` so that SimStack can find the associated files and results.

### The Solution: Patching and Re-execution
The strategy involves patching the "inner" execution function during the test run.

1. **Setup**: The node is first executed normally (or a previous execution is identified). All of the complex and costly computations are performed
   in a function like `complex_task_execution` which **must** have the signature *args, **kwargs.
    - The *args are used during normal execution to generate the costly data and serialize it.
    - The function returns data needed to locally construct the output models. Generating the input files or arsing the output files of a job, which is
      part of a test, should be outside `complex_task_execution`
    - The goal is: Do all the stuff that can be locally and that needs to be testes outside of `complex_task_execution
2. **Generation**: `generate_test` is called to create the test data.
3. **Patching**: Using `unittest.mock.patch`, the function responsible for the side effect (e.g., `complex_task_execution`) is replaced with
    a `patched_complex_task_execution`. This function also gets the *args and **kwargs. It needs the latter to automatically find
    the test directory with respect to a test-directory root.
4. **Mock Logic**: The patched function:
   - Identifies the correct test directory using `arg_hash` and node name (provided in `kwargs`).
   - The file can compare the input files generated in the test run with the input files in the test directory
   - Restores the necessary files (e.g., `test.txt`) from the test directory to the current execution environment.
   - Deserialize all important information to locally build the outputs of the nodes. In the test this is only the original `task_id`.

5. **Re-execution**: The node is called again with `force_rerun=True`.
6. **Verification**: The results of the re-execution are compared against the original results to ensure consistency.

Example Patch Implementation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    def patched_complex_task_execution(arg_patched, **kwargs_patched):
        # Locate the test directory
        test_dir = local_target_base / node_name / kwargs_patched["arg_hash"]
        
        # Restore state
        shutil.copy2(test_dir / "test.txt", Path.cwd() / "test.txt")
        
        # Retrieve original ID
        with open("test.txt", "r") as f:
            original_task_id = f.readlines()[1].strip()
            
        return original_task_id

    with patch("__main__.complex_task_execution", side_effect=patched_complex_task_execution):
        test_result = complex_task_for_testing(arg, parameters=Parameters(force_rerun=True))
        # Assertions...

Benefits
--------
- **Reproducibility**: Easily reproduce complex node executions in a local environment.
- **Isolation**: Test node logic without triggering expensive or external side effects.
- **Verification**: Ensure that data processing remains consistent over time.
