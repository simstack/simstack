Recovery and Repair of Nodes
===========================

This document explains how to implement recovery mechanisms for failed nodes in SimStack.
The example `recovery_test.py` demonstrates how to handle nodes that might fail after producing some output, and how to recover that output using a recovery node.

Overview
--------

The recovery process involves three main components:

1. **The Failing Node**: A node that performs some work, writes output to a file, but then fails (e.g., due to an exception).
2. **The Repair Function**: A helper function that knows how to locate the output of a failed node and "repair" the result by collecting the files and marking it as successful.
3. **The Recovery Operation**: A server-side operation that selects failed task
   instances and applies the repair function through the control plane.

Implementation Details
----------------------

The example implementation can be found in `examples/testing/recovery_test.py`.

.. literalinclude:: ../../../examples/testing/recovery_test.py
   :language: python
   :linenos:

How it Works
------------

1. **sxt_failing_node**:
   This node simulates a failure. Even though it fails, it ensures that its progress (in this case, `result.txt`) is saved in its working directory.

2. **stx_repair_node**:
   This function takes a `NodeRegistry` entry. It constructs the path to the original node's working directory using `context.config.workdir`. If it finds the expected output files, it uses `node_runner.succeed()` to provide the missing data.

3. **sxt_recovery_node**:
   The historical example queried persistence from workflow code. That pattern is
   no longer supported. Task selection and state transitions belong to a
   server-owned recovery endpoint/repository service; a runner may only submit
   claim-scoped recovery results through the control-plane API. There is no
   direct-database fallback.

4. **Main Execution**:
   The `main` function first runs several instances of `sxt_failing_node`, which
   all fail. Recovery must then be initiated by the server-side recovery
   operation, not by injecting persistence into a runner.
