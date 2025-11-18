Overview
========

Simstack II
~~~~~~~~~~~

Simstack II V0.1 delivers a modern, python based workflow system where

* workflows are submitted via a :ref:`graphical user interface<using-gui-section>`
  which is auto-generated from the :ref:`python code <writing-gui-section>`.
* data is persisted for reuse by automatically storing arguments and
  results in a (non-sql) database
* workflows are
    * dynamically generated at run time,
    * can be nested,
    * executed on different remote resources (within the same workflow)* results, including interactive tables and graphs, are visualized in the web
  based GUI, which is automatically generated in python
* artifacts, such are tables and graphs can be generated during or after the
  workflow execution for data analysis
* :doc:`workflows <workflows>` are written as decorated plain python functions,


**Roadmap:** Future versions will (more or less in this order):

* integrate research data management
* provide tools to wrap other workflow systems
* implement artifact generation via the GUI
* enable data aggregation via the GUI
* enable workflow development via the GUI

The ideas that guided the development behind this approach are discussed in the following section, for
a quick start go to :doc:`installation` and start using Simstack II via the :ref:`graphical user interface<using-gui-section>`.


Motivation
~~~~~~~~~~

Viewed very broadly scientists generate tables and figures, which they publish in journals.
Simstack II is a tool to help scientists to orchestrate complex computational
protocols, in part by using high performance computing or distributed
resources. In the context of computational science, ,amy workflows environments
have been developed to help scientists to implement execute and
maintain these computational protocols.

.. TODO:: Add better references

Presently, scientists have a wide range of workflow environments at their
disposal, `Wikipedia  <https://en.wikipedia
.org/wiki/Workflow_management_system>`_ lists many different implementations, ranging from Airflow,
Luigi, Nextflow, Snakemake, and many more. Many of these systems are
file-based, i.e. the workflow is defined in a static file, which is then
interpreted by the workflow engine. These systems are very powerful and have
been successfully used in many scientific applications, in particular in
bioinformatics. However, these systems are not very flexible, i.e. the
workflow is defined in a static file, which is then interpreted by the
workflow engine. This makes it difficult to implement complex dynamic workflows.

Driven by the ongoing AI/ML revolution very powerful python-based workflow systems have been developed, for example
`covalent <https://github.com/AgnostiqHQ/covalent>`_, `prefect <https://github.com/PrefectHQ/prefect>`_ and
`pyiron <https://github.com/pyiron/pyiron>`_, to name a few. Many of these systems go beyond static, file-based
protocols and thereby offer the full flexibility of the python ecosystem to enable complex computations.

SimStack II aims to complement these systems by focusing on the visualization
and inspection of the generated data via an interactive user interface, an
aspect  which remains underdeveloped in many python-based workflow systems.
Our experience with SimStack I has been that there are two overlapping
communities, one of which is focused on the development of lobar workflows
by the other, much larger group of scientists would benefit from the use in
scientific applications, but lacks the software engineering skills, to tackle
the unavoidable complexities of complicated scientific software running on
HPC  resources.

SimStack II is meant to bridge the gap between these communities, which gives
workflow-developers more visibility for their work and which gives
workflow-users  access to these computational tools. In our experience with
SimStack I we have realized that most people who can develop real-life
workflows in a graphical user interface also have the skill to implement
the same workflow in a python-based workflow environment that provides tools
for workflow orchestration. For this reason, a graphical user interface for the **developement** of workflows may be
more of a burden, than a benefit.

On the other hand, many application scientist without a strong computational
background struggle to use these workflows in a python and or HPC environment. In addition, it seems useful to
consider that worklows are used in very different contexts: when used in IT environments, e.g. for server or
database maintenance, workflows are typically very stable and change little over time. The work in an evironment
EWAT = everything works all the time.

It is often overlooked that scientific workflows are used in a very different context, NEW = nothing ever works.
In science workflows are typically in a state of flux, often because the underlying scientific hypothesis for their use
evolve during the course of a scientific project. The most obvious result of this is that the workflows succeed technically,
but the results do not support the underlying scientific hypothesis. This complictes workflow development because it calls
for a re-analysis of the data, often in ways that were not foreseen when the workflow was originally developed.
SimStack II is intended to support scientist on this journey of discovery,
where workflows change essentially in every iteration. In the implementation of SimStack II we have have therefore
focused on the following design criteria:

* Worklows should be started, executed and monitored via a graphical user interface on a variety of resources
* Results should be persisted and easily accessible for re-analysis, ideally through the graphical user interface
* Workflow components should be easy to implement in python, but the results should be accessible via a graphical user
  interface without a detailed knowledge of the frontend architecture. These components are not developed in the GUI.
* There should be a limited functionality to generate new workflows by combining existing components in the GUI, but this is not the main focus of SimStack II.

.. include:: architecture.rst
