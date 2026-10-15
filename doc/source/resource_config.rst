Program Execution
=================

When developers write new nodes that execute external programs they cannot know the specifics of the resources on which these nodes are to be run. 
Simstack separates the program execution logic from the specifics of a particular resource. The ``ResourceConfig`` class handles resource-specific 
configurations from a ``config.toml`` file. It provides methods to retrieve program parameters, setup scripts, 
and run commands in a controlled environment (optionally using temporary directories).


The config toml is structured by resources, i.e. if you have resources hpc1 and small-computer and my-laptop sections starting with [hpc1], [small-computer], [my-laptop]
contain the data for each of these nodes. The convenience functions return the data for the actual resource. In principle you are totally free 
which information you store in ``config.toml`` for your own nodes, but the convenience functions implement often used features, such as

- using scratch space for program execution and where that is 
- startup scripts 
- the actual command to execute to run the programs

.. note::
   Non-runner contexts provide ``context.resource_config``. API-only workflow
   workers deliberately do not receive the operator's ``config.toml`` and do
   not expose this property. A workflow may instantiate ``ResourceConfig``
   using its own packaged configuration. Operator runtime profiles configure
   worker launchers; they are not a copy of the operator configuration for
   workflow code to read.


For a non-runner context, standard nodes can use:



.. code-block:: python

   from pathlib import Path
   from simstack.core.context import context

   input_files: List[str | FileStack] = [...]
   output_files: List[str | FileStack] = [...] 
   result = context.resource_config.run("program-name", input_files, output_files) 




ResourceConfig Class
--------------------

The ``ResourceConfig`` class is located in ``simstack.util.resource_config``.

.. autoclass:: simstack.util.resource_config.ResourceConfig
   :members:
   :undoc-members:
   :show-inheritance:

Initialization
~~~~~~~~~~~~~~

The ``ResourceConfig`` is initialized with a path to the configuration file (or directory containing ``config.toml``) and the name of the resource.

.. code-block:: python

   from pathlib import Path
   from simstack.util.resource_config import ResourceConfig

   config = ResourceConfig(Path("/path/to/project"), "local")
   config.program = "orca"
   

Methods
~~~~~~~

*   ``os``: Property that returns the operating system of the resource (defaults to "linux").
*   ``program``: Property to get or set the current program name.
*   ``setup(node_runner=None)``: Executes the setup scripts defined for the resource.
*   ``run(program_name=None, input_files=None, output_files=None, node_runner=None)``: Executes the program command, handling file transfers and temporary directories based on configuration.
*   ``get_program()``: Retrieves configuration for the current program.
*   ``get_setup_params()``: Retrieves setup configuration for the current resource.
*   ``get_postprocessing_params()``: Retrieves post-processing configuration.

Integration with NodeRunner
~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a ``node_runner`` instance is passed to ``setup()`` or ``run()``, it will use ``node_runner.subprocess()`` to execute commands, which provides better logging and integration with SimStack's task management.

String ``run_command`` and setup scripts are shell scripts. ``ResourceConfig``
passes them as a single argument to an explicit interpreter, never as a shell
string to ``NodeRunner``. The default is ``/bin/sh -c`` on POSIX and
``cmd.exe /d /s /c`` on Windows. Set ``shell = ["bash", "-c"]`` on the resource,
setup table, or program when Bash features such as ``source`` are needed.
Each setup script runs in its own process; exported variables do not persist
into later scripts or the run command. Put dependent setup and execution in
one script, or use the operator's explicit worker environment allowlist.

An argv array (for example ``run_command = ["orca", "orca.inp"]``) bypasses the
shell. A nonzero exit raises ``subprocess.CalledProcessError`` after the real
``NodeRunner`` has retained the process log.

With a real ``NodeRunner``, ``use_temp`` (or its historical alias ``use_tmp``)
creates a unique scratch directory inside the task workdir. An external
``tmp_base_dir`` does not enlarge the worker's filesystem boundary. Input and
output paths must stay relative to their task/scratch directory; nested paths
are supported. Cleanup removes only that invocation's scratch directory and
keeps task logs. Without a ``NodeRunner``, the configured scratch root retains
its standalone behavior.

Config.toml Structure
---------------------

The ``config.toml`` file is organized by resource name. Each resource can have several sections:

General Resource Settings
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: toml

   [resource_name]
   os = "linux"  # optional, defaults to "linux"

Setup Section
~~~~~~~~~~~~~

Contains scripts to be executed before any program run and configuration for temporary directories.

.. code-block:: toml

   [resource_name.setup]
   scripts = [
       "module load orca",
       "export MY_VAR=value"
   ]
   tmp_base_dir = "/path/to/scratch" # A path, with optional environment variables

Post-processing Section
~~~~~~~~~~~~~~~~~~~~~~~

Contains settings for cleanup after execution.

.. code-block:: toml

   [resource_name.post-processing] # or [resource_name.postprocessing]
   scratch_cleanup = true

Program Section
~~~~~~~~~~~~~~~

Defines configuration for specific applications on the resource.

.. code-block:: toml

   [resource_name.program.orca]
   use_tmp = true
   run_command = "orca orca.inp > orca.out"
 

Example config.toml
~~~~~~~~~~~~~~~~~~~

.. code-block:: toml

   [local]
   os = "windows"

   [local.setup]
   scripts = ["set USER=%USERNAME%"]
   tmp_base_dir = "C:\\Temp"

   [local.program.orca]
   use_tmp = true
   run_command = "orca orca.inp > orca.out"
