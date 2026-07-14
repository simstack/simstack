FireAndForgetRunner
====================

.. autoclass:: simstack.methods.fire_and_forget_runner.FireAndForgetRunner
   :members:
   :undoc-members:
   :show-inheritance:

The ``FireAndForgetRunner`` is a specialized ``NodeRunner`` utility designed to execute multiple independent node tasks in parallel, similar to ``MassRunner``. However, unlike ``MassRunner`` which aggregates all results into a single ``DataSet``, the ``FireAndForgetRunner`` persists each individual result immediately to the database as a ``FireAndForgetResult`` record.

Key Features
------------

* **Parallel Execution**: Leverages ``asyncio`` to run multiple nodes concurrently.
* **Concurrency Control**: Supports an optional ``max_concurrency`` parameter using an internal semaphore.
* **Immediate Persistence**: Saves a ``FireAndForgetResult`` to the database as soon as each individual node call finishes, without waiting for other tasks.
* **Result Mapping**: Automatically captures and stores input arguments and return values (supporting ``Model``, ``SimstackResult``, and lists of ``Model``) into a dictionary.
* **Call Path Tracking**: Records the full call path for each task, facilitating traceability.

Usage
-----

The ``FireAndForgetRunner`` is typically used within an ``async with`` block inside a parent node.

.. code-block:: python

    from simstack.methods.fire_and_forget_runner import FireAndForgetRunner
    from simstack.models import IntData

    @node
    async def my_parallel_manager(count: IntData, **kwargs):
        # Initialize FireAndForgetRunner with the target node
        async with FireAndForgetRunner(target_node, max_concurrency=5, **kwargs) as runner:
            for i in range(count.value):
                # Create tasks for individual inputs
                runner.create_tasks(IntData(value=i))
        
        # After the block, all tasks are finished and persisted individually
        return True

Each call to a node via ``FireAndForgetRunner`` creates a ``FireAndForgetResult`` entry in the database.

FireAndForgetResult Model
-------------------------

Each result is stored using the ``FireAndForgetResult`` model:

.. autoclass:: simstack.models.fire_and_forget_result.FireAndForgetResult
   :members:
   :undoc-members:
   :show-inheritance:

Fields:
^^^^^^^

* **call_path**: A string representing the concatenated path of the runner and the node (e.g., ``/my_parallel_manager/target_node``).
* **models**: A dictionary containing all input arguments (prefixed with ``arg_``) and output results (prefixed with ``result_``).
* **success**: A boolean flag indicating if the node call was successful.

Comparison with MassRunner
--------------------------

While both runners facilitate parallel execution, they serve different persistence needs:

* **MassRunner**: Best for batch jobs where you want a single consolidated ``DataSet`` at the end. It supports result recovery and caching based on argument hashes.
* **FireAndForgetRunner**: Best for scenarios where you want immediate visibility of results as they arrive, or when the number of tasks is extremely large and you want to avoid holding all results in memory/aggregated model before saving. It does not currently support the same recovery/caching mechanisms as ``MassRunner``.
