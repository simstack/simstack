Simstack II Setup
=================

UI Setup
--------

1. Register at ``simstack.int.kit.edu`` (inside the KIT network). This will create an **inactive** user.
2. Ask an administrator to configure your user databases and activate your account.
3. Copy your connection string and database names from **Profile → Workspace access**.
4. Add your resources in **Profile → Resources** and mark one as **Default resource**.

Example resources configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This example is for initial setup. Upload JSON replaces the saved list; omitted
resources and their tokens are deleted. For an existing account, add resources
individually or export and retain the complete list. Omit ``id`` for new resources.

.. code-block:: json

   [
     {
       "resource_str": "local-home",
       "hostname": "localhost",
       "workdir": "$HOME/simstack",
       "python_paths": [],
       "environment_start": "",
       "ssh_key": "$HOME/.ssh/id_rsa",
       "routes": [
         "int-nano"
       ],
       "queue": "default",
       "is_default": true
     },
     {
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

* Use Python 3.12
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

Normally, use the Core revision pinned by the project's ``uv.lock``. To test
Core ``develop`` instead, first update the dependency in this project checkout:

.. code-block:: bash

   uv add --no-sync "simstack @ git+https://github.com/simstack/simstack.git@develop"

This changes ``pyproject.toml`` and ``uv.lock``. Then install the dependencies:

.. code-block:: bash

   source ~/.bashrc
   uv sync --locked

.. _configuration-file:

Create ``simstack.toml``
~~~~~~~~~~~~~~~~~~~~~~~~

Create a ``simstack.toml`` file (placeholders shown below):

.. code-block:: toml

   [parameters]
   [parameters.general]
   use_db = true
   workdir_self = "<PATH_TO_SIMSTACK_DATA_DIR>"
   # these are parameters for one user for all hosts

   [parameters.db]
   database = "<DATABASE_NAME_FROM_PROFILE>"
   test_database = "<TEST_DATABASE_NAME_FROM_PROFILE>"
   connection_string = "<COMPLETE_CONNECTION_STRING_FROM_PROFILE>"

   [parameters.server]
   url = "https://simstack.int.kit.edu"
   token = "<TOKEN_FOR_THIS_RESOURCE>"

Where:

* ``<PATH_TO_SIMSTACK_DATA_DIR>`` is the path to the data directory created above (e.g. ``$HOME/simstack``)
* Copy the database names and complete connection string from **Profile → Workspace access**; keep URI query parameters such as ``authSource``.
* Keep the section name ``[parameters.server]``; ``[server]`` alone is not read.
* ``url`` is the SimStack server address used for file transfers, without ``/api``.
  It must be reachable from the runner; ``localhost`` works only on the server's own machine.
* ``<TOKEN_FOR_THIS_RESOURCE>`` is the runner access token from that server's UI:
  open **Profile → Resources**, select this resource, then **Generate token → Copy token**.

Each resource uses its own token. Runners transferring files between each other
must connect to the same server and user database. Restart them after changing these settings.


Initialize the system
~~~~~~~~~~~~~~~~~~~~~

The runner for the resource marked **Default resource** initializes the installed
workflow modules automatically. To include these project directories explicitly:

.. code-block:: bash

   uv run create_model_table --dir examples --dir applications
   uv run create_node_table --dir examples --dir applications

For an existing account, keep its assigned database names and connection string.




Configure Git identity (for automatic updates, required)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you enable the runner's automatic Git updates, configure your Git identity:

.. code-block:: bash

   git config --global user.email "<YOUR_EMAIL_ADDRESS>"
   git config --global user.name "<YOUR_NAME>"


Start the runner
~~~~~~~~~~~~~~~~

From ``$HOME/projects/simstack-model``:

.. code-block:: bash

   nohup uv run simstack_runner --resource "<RESOURCE_NAME>" --pull false &

Where ``<RESOURCE_NAME>`` is one of the resources you defined in the UI.

``--pull false`` keeps the runner from changing your checkout during setup.
Enable automatic updates only after saving your changes and setting the
resource's **Git branch** to the intended project branch.
