.. _workflows:

Workflows
=========

.. note::

    This section is intended for workflow developers but also helpful to users. A more detailed how-to for nodes is given in :ref:`developer-guide`

From Python to Workflows
------------------------

The following section is intended to guide developers who are familiar with python to transform their code
into Simstack II. We will introduce the concepts step by step, starting with a simple python "workflow".
Workflows are comprised of nodes, which are implemented as python functions.  


.. code-block:: python

    def adder(arg1: float, arg2: float) -> float:
        return arg1 + arg2

    def multiplier(arg1: float, arg2: float) -> float:
        return arg1 * arg2

    def add_multiply_python(arg1: float, arg2: float, arg3: float) -> float:
        add_result = adder(arg1, arg2) 
        multiply_result = multiplier(add_result, arg3)
        return multiply_result


In this code we have three nodes, `adder`, `multiplier`, and `add_multiply_python`. The first two nodes are simple
functions, the last function nests the 2 simple functions, making this function a template for a workflow.

To transform this code into a Simstack II workflow, we want to introduce minimal changes to the code above to accomplish
the following:

* One or more of the functions should be visible and startable in the GUI
* The results of the nodes should be persisted in the database and visible in the GUI
* The nodes should be executable on remote resources

To achievieve this, we need to make the following changes in the code:

* decorate the functions that should be visible in the GUI with the `@node` decorator
* ensure that all input and output types are registered Simstack document models
* specifiy where the nodes should be executed

For the specific example above the code looks as follows:

.. code-block:: python

    @node(parameters=Parameters(resource="my-remote-host", queue="slurm-queue"))
    def adder(arg1: FloatData, arg2: FloatData, **kwargs) -> FloatData:
        return FloatData(field_name="adder_result", value=arg1.value + arg2.value)

    @node(parameters=Parameters(resource="my-remote-host", queue="default"))
    def multiplier(arg1: FloatData, arg2: FloatData, **kwargs) -> FloatData:
        return FloatData(field_name="multiplier_result", value=arg1.value * arg2.value)

    @node
    def add_multiply_python(arg1: FloatData, arg2: FloatData, arg3: FloatData, **kwargs) -> FloatData:
        add_result = adder(arg1, arg2, **kwargs)  # note passing the kwargs!!!!
        multiply_result = multiplier(add_result, arg3, **kwargs)
        return multiply_result

The first thing you notice is that the datatypes have changed. Since int, floats etc appear a lot, Simstack provides 
models for these types (see: :mod:`simstack.models.base_types`).

The @node decorator embeds the simple python functions into a complex workflow class
(see: :func:`simstack.core.node.node` in module: :mod:`simstack.core.node`).

Install new workflow packages through the Server API, using the UI or
``simstack_workflow`` command described in :ref:`registering`. The installer
discovers their document models and decorated nodes and publishes the catalogue
for the authenticated user. Runners do not register database tables.
The nodes `adder` and `multiplier` would be executed on a runner running on "my-remote-host".
The adder would be submitted to the "slurm-queue" and the multiplier would be executed immediately in the foreground.
add_multiply_python would be executed on the default resource by a runner called `local`. You can change these defaults in the UI.
The relationship between the classes in the code, the tables in the database
and the representation of this data in the GUI, these models are discussed in :ref:`persisting-results-section`.

Here are some ground rules to write workflows in Simstack II:

.. important::
  - Export decorated nodes and document classes at module level. Their
    canonical module-qualified paths identify them, not their short names.
  - Declare each node input as a SimStack ``Model`` and declare its return
    contract. Positional and keyword Model inputs are supported.
  - ``**kwargs`` is optional. Use it when the function needs execution helpers
    such as ``node_runner`` or explicitly forwards execution metadata.
  - Install the package through the Server before submitting its nodes. No
    runner-side ``make_node_table`` or ``make_model_table`` call is required.

Beyond that nodes functions are normal python functions which can use the full flexibility of python.
The can call other functions or other nodes, but should **avoid "closures"**, such as global variables.
For this reason, node functions should **not be class members** unless the
method is intentionally part of a registered Simstack document model.

You can consult the documentation of this class to understand how this works but this is not required to write
workflows.

The choice, whether a function is a "normal" function or a node is influenced
by the following considerations:

- Only nodes are visible in the GUI
- only nodes can be executed automatically on remote resources
- nodes can call normal functions or other decorated nodes; nested API calls
  inherit the active execution claim without requiring ``**kwargs`` forwarding

Return Types
~~~~~~~~~~~~

In workflows, node results must be visible through the server and may be
processed by other nodes. Results must therefore be registered
:class:`simstack.models.document.Model` documents that can be validated,
visualized in the GUI, and transferred to nodes on other resources.

In order to encapsulate data processing, this essentially **precludes the use
of files as relevant results**. Many terminal nodes will call external
programs that return their output in file format. In Simstack, all human
readable content of these files should be **parsed into Simstack Model types**
that can be stored in the database. In many cases the output files of
terminal nodes will nevertheless be required input by other terminal nodes. In
order to facilitate processing, references to these files should be stored in
the output to avoid error-prone parsing and recreation.

THe following return types od node_functions are supported:

- any **registered** Model (:ref:`registering`),
- True or False, the indicating success or failure,
- a class derived from SimstackResult, in particular NodeRunner,
- None will result in failure

:class:`simstack.core.simstack_result.SimstackResult` provides a framework to
return more than one result, including files and info_files to the calling
function. The class NodeRunner is a child class of SimstackResult and
provides useful helper functions to manage results, files and logging
(see below).

The Noderunner Helper Class
~~~~~~~~~~~~~~~~~~~~~~~~~~~

:class:`simstack.core.node_runner.NodeRunner` instances provide
useful quality-of-life features from logging to
the management of results. In realistic applications, nodes generate a lot of
auxiliary data, such as log-files of external programs, that are not results,
but useful to keep track of the data-flow. To facilitate oversight over
nodes that are executed remotely, it is useful to capture the available
output of nodes, even when they fail. The NodeRunner
class offers some quality-of-life features for this. Each node is passed an
instance of NodeRunner in the \*\*kwargs.

You can add output by:

- adding any **registered** Models to the node_runner as attributes
- appending FileStack instances to node_runner.files
- appending FileStack instances to node_runner.info_files

**Node Example with NodeRunner**

.. code-block:: python

    @node
    def my_node(*args,**kwargs) -> SimstackResult
        # get the supplied node_runner first
        node_runner = kwargs.get('node_runner',None)
        if node_runner is None:
            raise ValueError('no node_runner')
        # your code here ....

        file_with_data = FileStack.from_local_file("my_important_file.dat")
        node_runner.files.append(file_with_data) # file_with_data will be added
                                                 # automatically to the result
                                                 # table of the NodeRegistry
                                                 # entry

        some_file_with_log_output = FileStack.from_local_file("my_log.txt")
        # will not be added to the result tables but to the info_files tab or the node and
        # can be inspected in the gui for potential problems or output
        # that has not been parsed.
        node_runner.make_info_files(some_file_with_log_output)

        if some_error:
            # this error will be caught in the calling function
            # which will post-process the results above this line
            # but set the task_status to TaskStatus.FAILED
            raise RuntimeError("Something went wrong")

        my_data = ArrayStorage(name="my cool numpy result")
        my_data.set_array(numpy_array)
        node_runner.my_data = my_data # will also be added to the result table

        return node_runner.succeed("happy ending") # node_runner is a child
                                                   # class of SimstackResult


.. note::
   NodeRunner.make_info_files(file1,file2,pattern1,pattern2) is a more clever way to append many files to the info_files list.
   NodeRunner.info_file_patterns defines a standard set of endings to include automatically

.. note::
  The instance of NodeRunner passed from the calling function, will be
  post-processed by the calling function even when the node fails. This allows
  capturing of partial output, in particular the info_files.

.. note::
  The call to your actual function is always wrapped in a try-except-block.
  In most scenarios you do not need your own try-except-finally blocks in the
  node



**Data Provided by \*\*kwargs**

.. code-block:: python

    node_runner = NodeRunner(self._func.__name__, None, task_id=self.id)
    node_kwargs = {
        'node_runner': node_runner,
        'parent_id': self.id,                # DO NOT CHANGE & pass to children

        'task_id': self.id,                  # DO NOT CHANGE & pass
        'call_path': self.call_path,         # DO NOT CHANGE &
        'parent_parameters': self.parameters,   # this must have a name different from parameters, because
                                                # otherwise this setting will override all the parameters of
                                                # the child nodes
        'recompute_artifacts': self.recompute_artifacts,
        'custom_name' : self.custom_name
    }

    if self.parameters.force_rerun:
        node_kwargs['force_rerun'] = True

**Logging**

In order to keep track of node execution, Simstack core modules and user code
should write logs. Runner processes do not install a database log handler;
control-plane log delivery and task commits go through the server API. In node
code you can use a standard logger and the ``NodeRunner`` helpers:

.. code-block:: python

    import logging
    logger = logging.getLogger("name")

    @node
    def my_node(*args,**kwargs) -> SimstackResult

        node_runner = kwargs['node_runner'] # should never fail

        # Standard process log output
        logger.info("Some message I will never find")

        # this will be shown in a special tab called task-logs which collects
        # only messages for this node
        logger.info(f"task_id: {node_runner.task_id} informative message")

        # node_runner will add the task_id info
        node_runner.info("another informative message")

The node_runner helper functions are provided for all log-levels.

.. note::
    Simstack generates a lot of log messages. Use the task_id mechanism as
    much as possible to provide useful info

**Executing external programs**

The NodeRunner class provides :func:`simstack.core.NodeRunner.subprocess` to
facilitate execution of external programs. This helper function will execute
a command in the current shell and automatically add standard output and
error to the info_files of the node. The function returns True/False upon
success and failure. Stdout and sterr of the last command are also stored in
attributes so that they can be parsed by the node.

Node Decorator Arguments
~~~~~~~~~~~~~~~~~~~~~~~~
The node decorator may have arguments.

- Parameters: :class:`simstack.models.Parameters` where and how shall the node
  be executed
- force_rerun: force execution of the node, but not the children
- recompute artifacts: without executing the node or its children recompute
  all artifacts.

The :class:`simstack.models.parameters.Parameters` can specify

* a resource :class:`simstack.models.parameters.Resource` which must be one
  of the resource names listed in the configuration file(:ref:`configuration-file`).
  Specifying the resource ``self`` (the default) means that the node will be
  executed on the same resource as the calling node.
* a queue (makes sense only for remote resources), with values ``default`` for
  immediate execution, ``slurm_queue`` for submission to the batch system by
  slurm

.. note:

  The parameters are passed to the node as ``parent_parameters`` in the
  \*\*kwargs because terminal nodes may need the memory settings etc for
  mpi or other values

The slurm parameters are specified in Parameters has an attribute
``slurm_parameters`` of type: :class:`simstack.models.parameters.SlurmParameters`.

When specifying parameters in the @node decoration, the developer should
specify sensible default parameters for node execution. These can be overwritten
on-the-fly when calling the node:

.. code-block:: python

    @node(parameters=Parameters(resource="standard-resource",
    queue="slurm-queue"))
    def some_node(*args,**kwargs):
        .....


    def calling_function():
        # we are already in a slurm job, we dont want to start another
        # this node will run on the same resource
        some_node(*args,parameters=Parameters(resource="self"))


Node Execution
--------------

The node decorator wraps the actual execution of the function into a series
of operations controlled by the class Node.

- before execution all arguments and the function body are hashed using
  :func:`simstack.core.hash.complex_hash_function`, which will infer how to
  deal with nested objects, such as list, dicts, classes, etc.
  A domain model's ``complex_hash`` method overrides the default behavior.
  :class:`simstack.models.document.ObjectId` values are excluded where identity
  must not change the content hash.
- A completed NodeRegistry entry is reused only for the same input content and
  loaded implementation fingerprint (including the declared node version).
  Pending entries must also match the execution route; ``force_rerun`` bypasses
  reuse. The worker establishes initially unknown root identity through its
  claim-scoped API before execution.
- if no such NodeRegistry entry exists a new one is created with TaskStatus.SUBMITTED
- the server atomically claims remote work for a runner. The runner fetches a
  claim-scoped task bundle, executes the declared function, and commits results
  or failure through the server API.
- nested node calls made inside a runner are resolved through that same API;
  runner processes do not read or update ``node_registry`` directly.
- the caller obtains completed results from the server-owned persistence layer.

.. note::
  Timeouts for remote execution are not yet implemented.

Remote Execution
~~~~~~~~~~~~~~~~

Nodes scheduled for remote execution require ``simstack_runner`` on that
resource. In the UI, save the resource under **Profile → Resources**, use
**Generate token**, and **Download config.toml**. One file contains the Server
API URL and expected resource in ``[runner]``, plus basic local Python execution
settings in ``[execution]``. It contains no token or database configuration.
The API URL must be reachable from the execution machine.

Install the coordinated Core release in a Runner environment without MongoDB
drivers. Start it using the downloaded file:

.. code-block:: bash

    export SIMSTACK_RUNNER_TOKEN="<resource Runner token>"
    simstack_runner \
      --config /absolute/path/config.toml \
      --project-root /absolute/path/execution-root

The generated config provides the ``python`` workflow runtime profile. Its
``{runner_python}`` placeholder resolves to the running Runner interpreter,
without a machine-specific installation path. ``{python}`` in a packaged
worker command refers to that workflow revision's prepared environment.
Defaults execute basic Python work as the current user with an empty extra
environment allowlist. They do not add an operating-system network sandbox.
Operators can strengthen the guard or add cluster/container profiles in the
same file; existing explicit execution contracts are not replaced.

``SIMSTACK_RUNNER_TOKEN`` remains mandatory and is supplied through the
environment. The unified config's URL is authoritative. Its resource must match
both an explicitly supplied ``--resource`` and the token-bound Server bootstrap;
there is no fallback to another server or to a database when it fails.

Existing resource-keyed execution files with API URL/token environment variables
remain usable. ``--connection-config`` also remains available for older split
connection files, but is not needed for a new downloaded config. The sample
configuration generator writes the complete single ``config.toml`` by default:

.. code-block:: bash

    simstack_write_sample_config \
      --server-url https://example.org/api \
      --resource my-resource

The generator refuses to overwrite an existing file. Server bootstrap still
supplies resource, model-mapping and node-mapping metadata; local configuration
never supplies MongoDB credentials or replaces Server-owned control-plane data.

The execution ``--project-root`` must be a secret-free source snapshot because
workflow processes can read it, including through read-only container mounts.
Each local, container, Slurm, and watchdog launch contract must provide an
explicit ``worker_network_guard_command``. Deployments requiring network-level
isolation must replace the basic local pass-through guard with a verified
isolation command. The environment is responsible for enforcing its network
policy; a non-empty command alone does not establish isolation.

Deploy the Runner/Core installation through the deployment system. The package
installer separately delivers immutable workflow archives through the API and
prepares revision-local dependency environments under an operator-approved
runtime profile. It does not replace Core or mutate a live checkout.
``simstack_runner`` never stashes files, changes branches, pulls Git repositories
or synchronizes that checkout's dependencies.

.. _registering:

Registering Models and Nodes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The UI and CLI use the same authenticated ``/workflow-installations`` API.
The Server owns the user's catalogue and persistence; the Runner only receives
approved source archives and task/bootstrap contracts through the API. Installing
a workflow does not give it database credentials or a generic database API.

Package format
^^^^^^^^^^^^^^

Put ``simstack-workflow.toml`` at the archive root, alongside the importable
Python package. For example, ``example/nodes.py`` can contain:

.. code-block:: python

    from simstack.core.node import node
    from simstack.models.document import Model, Reference

    class Sample(Model):
        value: float

    class ScaleInput(Model):
        sample: Sample = Reference()
        factor: float = 2.0

    class ScaleOutput(Model):
        source: ScaleInput = Reference()
        value: float

    @node
    def scale(data: ScaleInput) -> ScaleOutput:
        return ScaleOutput(source=data, value=data.sample.value * data.factor)

Include ``example/__init__.py`` and this minimal manifest:

.. code-block:: toml

    [workflow]
    id = "org.example.scale"
    version = "1.0.0"
    modules = ["example.nodes"]
    runtime_profile = "python"

``id``, ``version``, ``modules`` and ``runtime_profile`` are required.
``source_root`` defaults to ``.``; use ``source_root = "src"`` for a ``src``
layout. Explicit ``dependencies`` are optional: when omitted they are read from
static ``[project].dependencies`` in ``pyproject.toml``, or are empty when there
is no project file. Dependencies are index requirements; uploaded build recipes,
URL/path requirements and database-driver dependencies are not supported.

Model schemas, ``Reference`` dependencies, canonical model/node mappings and
annotated input/output contracts are derived from Python. Do not duplicate them
as database tables or a hand-written schema registry. A single Model return has
the result name ``result``. ``SimstackResult`` returns require named result
declarations through the supported docstring contract or manifest node overrides.
Boolean returns also require an explicit document result contract; arbitrary
dictionaries and an unannotated return are not a substitute for it.

The installer checks archive containment, manifest structure and SimStack
interfaces. It imports the declared modules in an isolated runtime for discovery,
but does not call workflow node bodies, create fake datasets, run scientific
calculations or prove business correctness. Keep module imports free of workload
side effects. Ambiguous static child-call analysis and unavailable optional
``ui_schema()`` hooks appear as non-blocking preview warnings. Optional explicit
``called_nodes`` overrides can describe dynamic child calls.

UI installation
^^^^^^^^^^^^^^^

Open **Submit → Install workflows** in the left navigation. Choose a ZIP or use
the native folder picker, explicitly select target resources, and click
**Review package**.
The folder is uploaded as an archive, not as a local filesystem path. Review the
derived models, nodes, dependencies, revision and warnings, then click
**Confirm installation** to start delivery. ``staged`` is only a preview;
``installing`` is not ready to execute. An offline Runner leaves its resource
pending. The catalogue becomes active only after every selected resource is
ready. Open **Activity** to search/filter dated installation attempts and inspect
each resource's progress. A ready historical attempt is not necessarily the
currently published package revision. Use **Submit** separately to launch a workflow.

An interrupted ``validating`` job can be resumed into a preview; confirmation
to deliver it remains a separate action. A failed inspection requires a corrected
package upload. A confirmed job with failed resources can be explicitly retried
without reinstalling targets that are already ready.

CLI installation
^^^^^^^^^^^^^^^^

``simstack_workflow`` can run directly on a resource (or your computer) with Core
installed. It accepts the same ZIP or folder and uses a normal user bearer
token, not a resource Runner token. Supply an existing token via
``SIMSTACK_USER_TOKEN`` or ``--token-file``; the command does not save passwords
or tokens. The supplied ``--server-url`` is the API base URL verbatim. It does
not automatically append ``/api``: include a reverse-proxy prefix only if your
deployment uses one.

.. code-block:: bash

    simstack_workflow --server-url "$WORKFLOW_API_URL" --token-file /secure/user-token \
      upload ./my-workflow --resource local
    simstack_workflow --server-url "$WORKFLOW_API_URL" --token-file /secure/user-token \
      status INSTALLATION_ID
    simstack_workflow --server-url "$WORKFLOW_API_URL" --token-file /secure/user-token \
      confirm INSTALLATION_ID
    simstack_workflow --server-url "$WORKFLOW_API_URL" --token-file /secure/user-token \
      wait INSTALLATION_ID

Read the upload response and warnings before confirming. ``upload --confirm``
is an explicit non-interactive opt-in, not the default. Resource names are the
user's configured ``resource_str`` values, and ``--resource`` may be repeated.

Runtime profiles and revisions
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Each target resource needs an operator-configured profile matching the manifest's
``runtime_profile``. The profile provides ``installer_python``, worker launch
commands, a verified ``worker_network_guard_command`` and an environment
allowlist. A package cannot provide its own launcher or fall back to an old
per-node program when its profile is missing. See the Server README for the
separate required inspection sandbox configuration.

The Server stores the archive and versioned catalogue for the authenticated user.
Runners download it through a resource-scoped installation claim, verify its
SHA-256 revision, prepare an isolated environment and report readiness through
the API. Task bindings include package, installation ID, revision and profile.
Workers use that exact source snapshot; installing a newer version does not
silently redirect an existing task to the latest code. Core's operator-deployed
environment and the workflow's private dependencies remain separate.

Code-installation budgets are finite operator policy, not restrictions on
scientific workflow data. Server operators override shared defaults through
``SIMSTACK_WORKFLOW_INSTALL_LIMITS``; Runner profiles use an
``installation_limits`` table. Archive bytes, expansion, entry count, storage,
output and process budgets apply on both initial preparation and cache reuse.
The process boundary requires an unprivileged Linux UID and the configured
sandbox; it never falls back to unsandboxed execution on unsupported hosts.
The service/container must additionally have finite cgroup-v2 ``memory.max``
and ``cpu.max`` ceilings within ``max_process_tree_memory_bytes`` and
``max_process_tree_cpu_cores``. For the defaults, run the Server and Runner
containers with an unprivileged UID, ``--memory=4g --cpus=2`` (and an
operator-owned writable storage directory). This kernel boundary caps the
aggregate memory and CPU bandwidth of descendants; individual ``rlimit``
settings alone do not do that. Preparation fails closed when the required
quotas are absent or exceed policy. These are shared service/container budgets,
not independent per-installation cgroups: concurrent installations share them.
The wall-clock timeout still bounds elapsed runtime; ``cpu.max`` is a bandwidth
ceiling, not a lifetime CPU-seconds allowance for each installation.
Private dependencies may overlay ordinary libraries, but may not replace Core,
database namespaces, Python startup files or interpreter-startup hooks.

Most Common Errors & Problems
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* a deployment has not installed the intended immutable source snapshot (see
  the runner version reported by the deployment and Health Check)
* changing branch metadata in the GUI does not mutate a running checkout; the
  deployment system must build, validate, and activate the intended revision
* an installation is only staged, or one of its selected resources is not ready
* when you develop both an application package and Simstack core, update and
  validate the application's dependency lock in CI before deploying the new
  snapshot; the runner itself will not repair or synchronize that environment
* node or model not registered in the database (see :ref:`registering`)
* node or model not imported correctly (see below)
* missing or mismatched operator runtime profile; there is no legacy fallback
* relative imports suggested by IDE:

.. code-block:: python

    from simstack.models.document import Model
    from simstack.core.node import node


make sure all imports are relative to simstack. For example:

.. code-block:: python

    from simstack.models import Parameters
    from simstack.core.node import node

or relative the other top-level modules. e.g. applications
