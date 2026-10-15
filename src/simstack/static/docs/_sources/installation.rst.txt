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

Install the runner from the locked production dependency set. Do not start a
runner from a development/test environment: that group intentionally contains
MongoDB test packages and runner startup rejects it.

.. code-block:: bash

   source ~/.bashrc
   uv sync --locked --no-dev

The production environment must not contain ``bson``, ``motor``, ``odmantic``,
or ``pymongo``. Test dependencies belong in a separate virtual environment and
must never be used to launch ``simstack_runner``.


.. _configuration-file:

Prepare the execution checkout
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The path passed as ``--project-root`` is readable by workflow code and is
mounted read-only into Docker/Apptainer workers. It must therefore be a
secret-free execution snapshot. Do not put ``simstack.toml``, ``.env`` files,
database URLs, private keys, or unrelated credentials in that tree. A
read-only mount prevents writes; it does not hide readable secrets.

Runner-side ``config.toml`` contains only explicit process launch contracts.
Every local, container, Slurm, and watchdog execution path requires a
non-empty ``worker_network_guard_command`` and a
``worker_environment_allowlist``. The guard runs immediately before workflow
code inside the selected execution boundary. The runner can require this
contract but cannot prove that an arbitrary command (for example ``true``)
actually isolates a network; production deployment must supply and test a
real OS/container/HPC policy that prevents workflow access to database
endpoints and other forbidden egress.

No runner-side database configuration exists. Resource metadata, mappings,
task payload limits, claims, status, logs, results, and file-transfer state are
obtained from the authenticated server API.

Model/node catalogue administration and database migrations are server-side
operations. A runner token is not a database credential and cannot perform
those operations.




Deploy updates outside the runner
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``simstack_runner`` never runs ``git``, changes branches, stashes local files,
or invokes a dependency manager. It also does not inspect a particular lock
file or per-user extras file. Build and validate the secret-free source
snapshot and production environment in CI or a deployment supervisor, then
replace the deployment and restart the runner explicitly. A failed update must
leave the currently running deployment untouched.


Start the runner
~~~~~~~~~~~~~~~~

From ``$HOME/projects/simstack-model``:

.. code-block:: bash

   export SIMSTACK_SERVER_URL="https://simstack.example"
   export SIMSTACK_RUNNER_TOKEN="<RESOURCE_SCOPED_RUNNER_TOKEN>"
   nohup .venv/bin/simstack_runner \
     --resource <RESOURCE_NAME> \
     --project-root "$PWD" &

Where ``<RESOURCE_NAME>`` is one of the resources you defined in the UI.
Startup fails closed when credentials, bootstrap capabilities, launch commands,
the network guard, or required allowlisted worker variables are missing. If a
workflow needs a non-SimStack variable such as a licence path, preserve it
explicitly with a repeated ``--allow-environment NAME`` argument; arbitrary
ambient variables are removed from the supervisor environment.

There is no ``--no-pull`` switch: no pull or self-update path exists in the
runner.



Ignore after this line
----------------------

Notes / scratch commands:

.. code-block:: bash

   git submodule add -b new-init https://git@github.com/simstack/simstack.git simstack

.. code-block:: bash

   uv lock --upgrade-package <package-name>
