Recovery and Repair of Nodes
============================

This document explains how to implement recovery mechanisms for failed nodes in SimStack.
The example `recovery_test.py` demonstrates how to handle nodes that might fail after producing some output, and how to recover that output using a recovery node.

Overview
--------

The recovery process involves three main components:

1. **The Failing Node**: A node that performs some work, writes output to a file, but then fails (e.g., due to an exception).
2. **The Repair Function**: A helper function that knows how to locate the output of a failed node and "repair" the result by collecting the files and marking it as successful.
3. **The Recovery Node**: A node that consumes explicit task references and published output files, and produces a repaired result under its own execution.

Implementation Details
----------------------

The historical ``recovery_test.py`` example used direct task writes and is not
an executable example for the current API boundary. Recovery code must use
the task API and explicit scientific value publication.

How it Works
------------

1. **sxt_failing_node**:
   This node simulates a failure. Even though it fails, it ensures that its progress (in this case, `result.txt`) is saved in its working directory.

2. **stx_repair_node**:
   This function takes a `NodeRegistry` entry. It constructs the path to the original node's working directory using `context.config.workdir`. If it finds the expected output files, it uses `node_runner.succeed()` to provide the missing data.

3. **sxt_recovery_node**:
   Read a known task with ``await context.server.tasks.get_task(task_id)`` and
   resolve its scientific references with ``context.server.values.resolve_values``.
   A recovery node publishes repaired outputs under its own task. Preserve the
   original failure and its error; scientific publication cannot change a task's
   status. Retrying the original task must go through the Server task lifecycle.

4. **Main Execution**:
   The `main` function first runs several instances of `sxt_failing_node`, which all fail. Then, it runs `sxt_recovery_node` to automatically fix these failures and recover the data.
