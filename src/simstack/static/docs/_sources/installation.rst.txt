Simstack II Setup
=================

UI Setup
--------

1. Create an account on your SimStack Server and have it activated by its administrator.
2. Create or select a resource in the UI and run its enrollment command on that resource.
3. Connect the workflow repository and select its branch. Server publishes the catalogue and Runner prepares the pinned revision.

Database credentials are configured by the Server administrator and are never
issued to Runner or workflow environments.

Install the Runner
------------------

Choose a writable work directory in the resource settings. For this example,
name the resource ``workstation``. On the worker, run the enrollment command
shown in the UI. The installer installs Core and starts the Runner. Its output
gives the path to ``runner.log``. The same directory contains ``credentials.json``.
Private workflow repositories use the workspace SSH key registered on the Git host.

.. _configuration-file:

Create ``simstack.toml``
~~~~~~~~~~~~~~~~~~~~~~~~

Use the Resources page to restart a running managed Runner. For a manual
foreground launch after the enrolled Runner has stopped, generate the optional
machine configuration in your chosen directory. Replace ``<slug>`` below with
the directory name shown in the installer's log path.

.. code-block:: bash

   runner_dir="$HOME/.local/share/simstack/runners/<slug>"
   "$runner_dir/bin/simstack_write_sample_config"
   "$runner_dir/bin/simstack_runner" --credentials "$runner_dir/credentials.json" \
       --project-root "$runner_dir/runtime" --config "$PWD/simstack.toml"

The generated ``[workstation.program.python]`` section is a machine-local
command override. Use your enrolled resource name for that section.
Server supplies the resource's work directory and Python
paths. ``SIMSTACK_RUNNER_WORKDIR`` overrides the directory on this machine.
The credentials file supplies the Server URL and Runner token. MongoDB and
S3 credentials belong to Server.

For a Python user session, initialize ``context`` with ``server_url``,
``user_token`` and the selected resource, or supply
``ServerAPI.for_user(server_url, user_token, resource=...)`` as ``server``.
A user access token is distinct from a Runner resource token.


Initialize the system
~~~~~~~~~~~~~~~~~~~~~

Connect the workflow repository in the UI. Server discovers its model/node
catalogue and registers the selected revision. Runner prepares that revision
and reports readiness before tasks are admitted.

The ``create_model_table`` and ``create_node_table`` commands remain local
catalogue inspection tools. They collect descriptors and do not write a
database from Runner.
