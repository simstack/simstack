Testing Complex Nodes
=====================

Some nodes depend on external services, large files, or non-deterministic work
that should not be repeated in every test. SimStack can capture a successful
node execution and replay the inexpensive parts with the external side effect
patched out.

The workflow has two parts:

#. :mod:`simstack.methods.generate_test` extracts the node inputs, outputs, and
   work-directory files into a stable test directory.
#. :file:`examples/testing/testing_test_generation.py` shows the patch-and-replay
   pattern used by a generated test.

Generating test data
--------------------

The ``create_test_from_node`` command accepts a ``NodeRegistry`` ID and creates
a directory arranged as ``target_root / node_name / arg_hash``. It copies the
node work directory and writes two convenience files:

* ``inputs.json`` contains models referenced by ``input_references``.
* ``outputs.json`` contains models referenced by ``results_references``.

Reference keys retain the captured ``variable_name``. This is important when
several arguments or results use the same model type.

Patching external work
----------------------

Keep local parsing and result construction outside the expensive helper. The
helper should accept ``*args`` and ``**kwargs`` so a replay function can use the
captured ``arg_hash`` to locate the matching test data.

During replay:

#. Patch the expensive helper with :func:`unittest.mock.patch`.
#. Restore any captured files needed by the local parsing code.
#. Return the captured identifier or intermediate result.
#. Invoke the node with ``Parameters(force_rerun=True)``.
#. Compare the reconstructed outputs with the captured outputs.

Minimal replay helper
~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: ../../examples/testing/testing_test_generation.py
   :language: python
   :linenos:

This keeps the test deterministic while still exercising the node's argument
handling, local processing, and output construction.
