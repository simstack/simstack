Overview
========

SimStack II
~~~~~~~~~~~

SimStack II is a modern, Python-based workflow system with a web-based graphical user interface (GUI). The GUI is generated automatically from your Python workflow code and supports interactive inspection of inputs, outputs, tables, and plots.

Key features
^^^^^^^^^^^^

* **GUI-driven execution:** Workflows are submitted via a :ref:`graphical user interface <using-gui-section>`, which is auto-generated from the :ref:`Python code <writing-gui-section>`.
* **Persistent results:** Inputs and results are stored automatically in a non-SQL database so they can be reused and re-analyzed.
* **Flexible workflows:** Workflows can be generated dynamically at runtime, nested, and executed across different remote resources.
* **Artifacts for analysis:** Tables, plots, and other artifacts can be created during or after workflow execution to support data analysis.
* **Simple authoring model:** :doc:`Workflows <workflows>` are written as decorated plain Python functions.

Roadmap
^^^^^^^

Future versions are planned to (roughly in this order):

* integrate research data management
* provide tools to wrap other workflow systems
* implement artifact generation via the GUI
* enable data aggregation via the GUI
* enable workflow development via the GUI

For a quick start, see :doc:`installation` and start using SimStack II via the :ref:`graphical user interface <using-gui-section>`.

Motivation
~~~~~~~~~~

Computational scientists produce data that ultimately becomes figures and tables in publications.
To aid this process many workflow environments already exist:
`Wikipedia <https://en.wikipedia.org/wiki/Workflow_management_system>`_ lists a wide range of systems
such as Airflow, Luigi, Nextflow, and Snakemake. Many widely used tools are file-based:
a workflow is defined in a static configuration file and executed by an engine.
This approach can be robust and has been very successful,
but it often becomes limiting when workflows must be assembled dynamically or adapted during execution.

In parallel, the AI/ML ecosystem has accelerated the development of powerful Python-native workflow
systems such as
`covalent <https://github.com/AgnostiqHQ/covalent>`_,
`prefect <https://github.com/PrefectHQ/prefect>`_, and
`pyiron <https://github.com/pyiron/pyiron>`_.
These systems embrace Python directly, enabling more dynamic and expressive computation pipelines.

Our experience with SimStack I suggests there are two only partially overlapping communities:

* expert workflow developers (in python or otherwise) who struggle to develop user-interfaces
make these workflows accessible to end-users
* a larger group of workflow users, but may not have the software engineering background
to develop the workflows on their own.

Bridging developers and users
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

SimStack II aims to bridge this gap by making workflow execution and result exploration accessible
through a GUI, while keeping workflow implementation in Python. This enables rapid development of
workflows using the full power of the python ecosystem. We believe this approach is much more
powerful than attempting to construct complex workflows in a graphical user interface.

Developers who can construct workflows via a GUI typically have the ability to
write them directly in Python. A GUI for *workflow development* thus adds overhead
without providing much benefit. A GUI for *workflow execution and analysis*,
however, can substantially lower the barrier for many users.

In developing a workflow system for scientific applications, we also need to consider another
issue: Workflows in IT settings (e.g., server or database maintenance) tend to be stable:
the goal is an environment where everything works all the time.

Scientific workflows often operate in a different environment: methods, hypotheses, and
analysis requirements evolve continuously. A workflow may succeed technically while
producing results that invalidate the motivating hypothesis—triggering
new analysis and iteration.

This reality makes persistence, traceability, and interactive re-analysis essential.

Design goals
^^^^^^^^^^^^

SimStack II is designed with the following priorities:

* Workflows are initialized, monitored, and managed through a GUI on a variety of compute resources.
* Results are persisted and accessible for re-analysis, ideally through the GUI.
* Workflow components are implemented in Python, with minimal coding overhead.
* Workflows expose results in a UI that is (to a large extent)
automatically generated on the basis of the python code additional frontend development.

.. include:: architecture.rst
```