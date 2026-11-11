Simstack II Setup
=================

UI Setup
--------

1. Create an account on your SimStack Server and have it activated by its administrator.
2. Create or select a resource in the UI and run its enrollment command on that resource.
3. Connect the workflow repository and select its branch. Server publishes the catalogue and Runner prepares the pinned revision.

Database credentials are configured by the Server administrator and are never
issued to Runner or workflow environments.

Example resources configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: json

   [
     {
       "id": "<object-id>",
       "resource_str": "local-home",
       "hostname": "localhost",
       "workdir": "$HOME/simstack",
       "python_paths": [],
       "environment_start": "",
       "ssh_key": "$HOME/.ssh/id_rsa",
       "routes": [
         "int-nano"
       ],
       "queue": "default"
     },
     {
       "id": "<object-id>",
       "resource_str": "int-nano",
       "hostname": "int-nano.int.kit.edu",
       "workdir": "$HOME/simstack",
       "python_paths": [],
       "environment_start": "",
       "ssh_key": "$HOME/.ssh/id_rsa",
       "routes": [],
       "queue": "default"
     },
     {
       "id": "<object-id>",
       "resource_str": "justus",
       "hostname": "justus2.uni-ulm.de",
       "workdir": "$HOME/simstack",
       "python_paths": [],
       "environment_start": "",
       "ssh_key": "$HOME/.ssh/id_rsa",
       "routes": [
         "int-nano",
         "local-home"
       ],
       "queue": "slurm-queue"
     }
   ]


Installation of Runners
-----------------------

Prerequisites
~~~~~~~~~~~~~

* Install ``uv``:
  https://docs.astral.sh/uv/getting-started/installation/
* Create directories:

  * ``$HOME/simstack``
  * ``$HOME/projects``

Clone the repository
~~~~~~~~~~~~~~~~~~~~

In ``$HOME/projects`` clone the repository:

* SSH (if your SSH key is added to GitLab):

  .. code-block:: bash

     git clone git@gitlab.kit.edu:kit/ag_wenzel/simstack-model.git

* HTTPS (if you have a token):

  .. code-block:: bash

     git clone https://gitlab.kit.edu/kit/ag_wenzel/simstack-model.git

Then:

.. code-block:: bash

   cd simstack-model


Host-specific environment notes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On ``int-nano`` you may need to override the C/C++ toolchain because the default compiler is too old.
Add the following to your ``~/.bashrc``:

.. code-block:: bash

   export PATH=/path/to/gcc-12.3/bin:$PATH
   export LD_LIBRARY_PATH=/path/to/gcc-12.3/lib64:$LD_LIBRARY_PATH
   export CC=/path/to/gcc-12.3/bin/gcc
   export CXX=/path/to/gcc-12.3/bin/g++

Set the paths to the location where gcc-12.3 is installed (e.g. ``/shared/user/ww`` or ``/home/ws/<user>``).
It is unclear what you need on other systems.

On ``justus`` you may need:

.. code-block:: bash

   export LD_LIBRARY_PATH=$HOME/local/lib:$MKLROOT/lib/intel64:$LD_LIBRARY_PATH


Sync dependencies
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   source ~/.bashrc
   uv sync --locked

Core depends on the Server API rather than a database driver. Storage integration tests run with the sibling Server checkout and its development dependencies.


.. _configuration-file:

Create ``simstack.toml``
~~~~~~~~~~~~~~~~~~~~~~~~

Create a ``simstack.toml`` file (placeholders shown below):

.. code-block:: toml

   [parameters.server]
   url = "https://<SIMSTACK_SERVER>"

Runner receives its resource token through enrollment. Set
``SIMSTACK_RUNNER_TOKEN`` and ``SIMSTACK_RUNNER_WORKDIR`` in its environment;
MongoDB and S3 credentials belong only to the Server deployment. Do not add a
``[parameters.db]`` section or a database connection string to workflow or
Runner configuration.

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




Configure Git identity (required)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Because the runner performs Git operations, ensure you have:

.. code-block:: bash

   git config --global user.email "<YOUR_EMAIL_ADDRESS>"
   git config --global user.name "<YOUR_NAME>"


Start the runner
~~~~~~~~~~~~~~~~

From ``$HOME/projects/simstack-model``:

.. code-block:: bash

   nohup uv run simstack_runner --resource <RESOURCE_NAME> &

Where ``<RESOURCE_NAME>`` is one of the resources you defined in the UI.



Ignore after this line
----------------------

Notes / scratch commands:

.. code-block:: bash

   git submodule add -b new-init https://git@github.com/simstack/simstack.git simstack

.. code-block:: bash

   uv lock --upgrade-package <package-name>