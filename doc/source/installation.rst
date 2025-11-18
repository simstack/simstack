
============
Installation
============

Install Simstack II — 3-step lightning setup ⚡️
=================================================

**Goal:** create an isolated ``simstack_ii`` environment, pull the model code, and satisfy every dependency in a single coffee break.

+-------------------------------------------+----------------------------------------------------+
| What you'll do                            | Why it matters                                     |
+===========================================+====================================================+
| 1 Create a *conda-style* environment     | Keeps your system Python pristine                 |
+-------------------------------------------+----------------------------------------------------+
| 2 Install the exact libraries in         | Reproducible science → zero "works-on-my-machine" |
|   ``requirements.txt``                   | bugs                                               |
+-------------------------------------------+----------------------------------------------------+
| 3 Clone the **files** branch of          | Gives you ready-made tasks, sample data, and      |
|   **simstack-model**                     | tests                                              |
+-------------------------------------------+----------------------------------------------------+

Setting up runners 🐍
-------------------

* Install pixi: https://pixi.sh/latest/installation/
* Clone the git repository: https://gitlab.kit.edu/kit/ag_wenzel/simstack-model
* if you are developing: switch to your branch


.. note:: **micromamba – single-file binary**

   .. code-block:: bash

      micromamba create -n simstack_ii python=3.12 -y
      micromamba activate simstack_ii

.. note:: **Classic conda**

   .. code-block:: bash

      conda create -n simstack_ii python=3.12 -y
      conda activate simstack_ii

.. important::
   **Heads-up**: Simstack II works with Python ≥ 3.12 (CPython 64-bit).
   Older versions (<3.12) may miss tomllib support and fail at runtime.

Step 2 — Install dependencies 📦
---------------------------------

.. code-block:: bash

   # activate the (simstack_ii) env
   python -m pip install --upgrade pip
   pip install -r requirements.txt

Step 3 — Grab the Simstack-Model repository 🛰️
-----------------------------------------------

.. code-block:: bash

   git clone \
     --branch main \
     https://gitlab.kit.edu/kit/ag_wenzel/simstack-model.git

    git checkout my_branch

.. important::
    * The branch main is the standard branch to be used by the server
    * Create your own branch to work in because main is protected
    * Make sure all runners are on the same branch

Type ``tree`` in the terminal, if the **installation** succeeds, you should
see a directory structure like the folder tree shown below.

.. code-block:: text

   📂 simstack-model
   ├── 📁 conda-recipe
   ├── 📁 doc
   │   └── 📁 source
   │       └── 📁 resources
   ├── 📁 projects         # install your personal node and functions here
   │   └── 📁 my_project
   📁 applications         # install persistent community contibutions here
   │   └── 📁 orca_results
   ├── 📁 scripts
   ├── 📁 src
   │   ├── 📁 logs
   │   └── 📁 simstack
   │       ├── 📁 core
   │       ├── 📁 methods
   │       │   ├── 📁 electronic_structure
   │       │   │   └── 📁 orca
   │       │   └── 📁 group
   │       │       └── 📁 battery_kmc
   │       ├── 📁 models
   │       ├── 📁 server
   │       │   └── 📁 routes
   │       └── 📁 util
   └── 📁 tests
       ├── 📁 core
       ├── 📁 methods
       └── 📁 server


.. _configuration-file:

Configure Simstack II with ``simstack.toml`` ⚙️
================================================

Simstack II reads a single **TOML** file (``simstack.toml``) to learn

* which **resources** (local & remote) exist,
* how to reach your **MongoDB** backend,
* and where each host should place logs / artifacts.

.. note::
   **Where should the file live?**
   Save it next in the folder simstack-model in both your local and HPC accounts.
   The CLI searches those paths automatically.

Minimal template
----------------

.. code-block:: toml

    #######################################
    # Global / shared parameters
    #######################################
    [parameters]    # these are parameters for one user for all hosts
    [parameters.common]
    resources = ["local", "tests", "resource1", "resource2",  "self", "uploads"]
    database = "user_data"
    test_database = "user_test"
    #connection_string="mongodb://user:PASSWORD@SERVER:27017/"
    # these parameters must be adapted for each host
    [parameters.self]
    ssh-key = XXXXX # path to your private key
    resource = "local" # resource the runner on your computer will use
    workdir = XXXXX # path to your simstack working directory
    python_path = [ XXXXX,YYYY ] # python path to all required packages
    environment_start = XXXX # command to start the environment simstack is
                             # installed in
    [parameters.local]
    .....
    [parameters.tests]
    .....
    [parameters.resource]
    ssh-key = "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh"  # path to your private key
    resource = "self" # resource the runner on your computer will used
    workdir = "C:\\Users\\bj7610\\simstack" # path to your simstack working directory
    python_path = [ "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model",
                   "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src"]
    [parameters.int-nano]
    ssh-key = "/home/ws/bj7610/.ssh/id_rsa"  # path to your private key
    workdir = "/home/ws/bj7610/simstack" # path to your simstack working directory
    python_path = [ "/home/ws/bj7610/projects/simstack-model",
                   "/home/ws/bj7610/projects/simstack-model/src"]
    environment_start = "conda activate simstack-env"
    # normal users do not have to change anythign below this line
    # these are the parameters for the database server
    [server]
    port = 8000
    secret_key="61617e60e68230462fa89eef2db43d65fc2341cd281ce4cc6eb7609345bbe42d"
    upload_dir = "C:\\Users\\bj7610\\simstack\\uploads"
    # these are the parameters for the overall configurations
    [hosts]
    local = "localhost"
    int-nano="int-nano.int.kit.edu"
    justus="justus.int.kit.edu"
    horeka="horeka.int.kit.edu"

    [[routes]]
    source = "local"
    target = "int-nano"
    host = "local"

    [[routes]]
    source = "int-nano"
    target = "local"
    host = "local"

    [[routes]]
    source = "horeka"
    target = "local"
    host = "horeka"

    [[routes]]
    source = "local"
    target = "horeka"
    host = "horeka"

    [[routes]]
    source = "justus"
    target = "local"
    host = "justus"

    [[routes]]
    source = "local"
    target = "justus"
    host = "justus"

    [paths]
    # Path configuration for the PathManager.
    # Each path entry should have a path and an optional drops value.
    # The path is the directory to search for Python files
    # The "drops" value is a prefix to drop from module names (for import paths)
    models = { path = "src\\simstack\\models", drops = "src" }
    methods = { path = "src\\simstack\\methods", drops = "src" }
    ui_testing = { path = "src\\simstack\\ui_testing", drops = "src" }
    examples = { path = "examples", drops = "", use_pickle = false }
    spectra = { path = "examples\\science\\electronic_structure\\spectra", drops = "", use_pickle = false }
    applications = { path = "applications", drops = "", use_pickle = false }
    tests = { path = "tests", drops = "", use_pickle = false }

