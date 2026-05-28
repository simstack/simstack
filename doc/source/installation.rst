Simstack II Setup
=================

UI Setup
--------

1. Register at ``simstack.int.kit.edu`` (inside the KIT network). This will create an **inactive** user.
2. Email the maintainer so your account can be activated and you can receive:

   * a database name
   * a database password

   (This is not automated yet.)
3. Go to your profile and upload your resources configuration.

Example resources configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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
   database = "<NAME>_data"
   test_database = "<NAME>_test"
   connection_string = "mongodb://<USER>:<PASSWORD>@<HOST>:27017/"
   mongodump_path = "<PATH_TO_MONGODUMP_EXE>"

Where:

* ``<PATH_TO_SIMSTACK_DATA_DIR>`` is the path to the data directory created above (e.g. ``$HOME/simstack``)
* ``<NAME>`` is your database name (often your first name in lower case)
* ``<PASSWORD>`` is the database password
* ``<PATH_TO_MONGODUMP_EXE>`` is the path to the ``mongodump`` executable (required for database backups)


Initialize the system
~~~~~~~~~~~~~~~~~~~~~

This will happen automatically when the default runner starts.

.. code-block:: bash

   uv run create_model_table --dir examples --dir applications
   uv run create_node_table --dir examples --dir applications

Note: this may crash if your database is very old.




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
```