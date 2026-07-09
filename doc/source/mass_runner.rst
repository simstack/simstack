MassRunner
==========

.. autoclass:: simstack.methods.mass_runner.MassRunner
   :members:
   :undoc-members:
   :show-inheritance:

The ``MassRunner`` is a specialized ``NodeRunner`` utility designed to execute a large number of independent node tasks in parallel. It handles task orchestration, concurrency control, and result persistence within a ``DataSet``.

Key Features
------------

* **Parallel Execution**: Leverages ``asyncio`` to run multiple nodes concurrently.
* **Concurrency Control**: Supports an optional ``max_concurrency`` parameter using an internal semaphore.
* **Automatic Persistence**: Stores results (both successful and failed) in a ``DataSet`` model.
* **Result Recovery**: Can recover results from previous runs of the same or different nodes using ``recover_orphaned_datasets``.
* **Hashing and Caching**: Computes hashes for arguments to skip redundant executions.

Usage
-----

The ``MassRunner`` is typically used within an ``async with`` block inside a parent node.

.. code-block:: python

    from applications.general.mass_runner import MassRunner
    from simstack.models import IntData

    @node
    async def my_parallel_master(argument: IntData, **kwargs):
        # Initialize MassRunner with the target node to be executed in parallel
        async with MassRunner(target_node, max_concurrency=10, **kwargs) as runner:
            for i in range(argument.value):
                # Create tasks for individual inputs
                runner.create_tasks(IntData(value=i))
        
        # After the block, runner.dataset contains all results
        return runner.dataset


The dataset will comprise a single ``DataSetSection`` called "tasks". Each row contains all inputs and outputs of the
node that has been called, success and error flags. The name of the row is the arg-hash of the arguments of the node.

.. :note:
   The rows returned by MassRunner may contain much more data than is actually needed in the subsequent calculation.
   It may therefore be better to immideatly create a derived dataset with only the relevent data.

.. :note:
   MassRunner is meant for jobs which are expected to mostly suceed and which take roughly the same amount of time



Detailed Flow in MassRunner Tests
---------------------------------

The ``mass_runner_test.py`` file demonstrates how ``MassRunner`` handles task execution, failures, and recovery.

1. **Failure Demonstration (mass_runner_test_master_node)**
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The test first calls ``mass_runner_test_master_node``, which uses ``MassRunner`` to call ``sad_node``.

*   **sad_node logic**: Fails if the input integer is even (``argument.value % 2 == 0``).
*   **Execution**: For inputs ``0, 1, 2``, inputs ``0`` and ``2`` will fail, while ``1`` will succeed.
*   **Dataset State**: The resulting ``DataSet`` contains a "tasks" section where each entry records the success status and any error messages.

2. **Recovery and Fix (mass_runner_test_fix_master_node)**
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The second part of the test demonstrates how to "fix" a failed workflow by reusing successful results and replacing failed ones with results from a different node (or a fixed version of the same node).

*   **Context Setup**: It passes the ``previous_task_id`` and sets ``previous_node_name`` in ``kwargs``.
*   **MassRunner Initialization**: It initializes ``MassRunner`` with ``happy_node`` (which always succeeds).
*   **Orphan Recovery**: Calling ``await result.recover_orphaned_datasets()`` searches the database for the dataset generated in the first step.
*   **Selective Execution**:
    - When ``create_tasks`` is called, ``MassRunner`` checks the existing dataset for a matching ``arg_hash``.
    - If a **successful** result is found for that hash, it is copied into the new dataset and the node execution is skipped.
    - If no result or a **failed** result is found, it executes the new node (``happy_node``) and stores the new success.
*   **Result**: The final dataset contains the reused success from the first run (for input ``1``) and the new successful results from ``happy_node`` (for inputs ``0`` and ``2``).

Logging and Metadata
--------------------

``MassRunner`` automatically attaches metadata to the ``DataSet``, including:
*   The original node name.
*   The task ID of the master node.
*   The argument hash used for grouping.
*   The call path within the workflow.

This metadata is crucial for the ``recover_orphaned_datasets`` method to correctly identify related data from previous executions.
