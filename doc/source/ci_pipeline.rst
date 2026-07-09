.. _ci_pipeline:

CI Pipeline and Documentation Artifacts
=======================================

The SimStack II project uses GitHub Actions for Continuous Integration. One of the jobs in the pipeline is ``package-html-docs``, which automatically generates the API documentation and builds the Sphinx HTML documentation for the installed package.

Where the HTML documentation lives
----------------------------------

The generated HTML documentation is owned by the ``simstack`` package and is stored under:

.. code-block:: text

   src/simstack/static/docs

This directory is included as package data. Any application that installs ``simstack`` can serve the generated HTML from the installed package instead of rebuilding or copying documentation from this repository.

How the CI job updates docs
---------------------------

On branch pushes and manual workflow runs, the ``package-html-docs`` job:

1. Runs ``sphinx-apidoc`` to refresh generated API documentation sources.
2. Builds Sphinx HTML into ``src/simstack/static/docs``.
3. Uploads the generated HTML as the ``sphinx-docs`` artifact for inspection.
4. Commits changed generated HTML back to the same branch.

The auto-commit message contains ``[skip ci]`` so the generated-docs commit does not start another CI run.

How other projects should consume docs
--------------------------------------

Other projects should not download CI artifacts or rebuild these docs. They should install the desired ``simstack`` revision and use/serve ``simstack/static/docs`` from the installed package.
