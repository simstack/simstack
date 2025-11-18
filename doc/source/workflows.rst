.. _workflows:

Workflows
=========

.. note::

    This section is intended for workflow developers

From Python to Workflows
------------------------

The following section is intended to guide developers who are familiar with python to transform their code
into Simstack II. We will introduce the concepts step by step, starting with a simple python "workflow".
Workflows are comprised of nodes, which are implemented as python functions.  A simple example for a pure python workflow
is illustrated below and in [examples/binary_operations.py](examples/binary_operations.py):


.. code-block:: python

    def adder(node_input: BinaryOperationInput) -> FloatData:
        return FloatData(value=node_input.arg1 + node_input.arg2)

    def multiplier(node_input: BinaryOperationInput) -> FloatData:
        return FloatData(value=node_input.arg1 * node_input.arg2)

    def add_multiply_python(node_input: AddMultiplyInput) -> FloatData:
        add_result = adder(BinaryOperationInput(arg1=node_input.a, arg2=node_input.b))
        multiply_result = multiplier(BinaryOperationInput(arg1=add_result.value, arg2=node_input.c))
        return FloatData(value=multiply_result)


In this code we have three nodes, `adder`, `multiplier`, and `add_multiply_python`. The first two nodes are simple
functions, the last function nests the 2 simple functions, making this function a workflow.

To transform this code into a Simstack II workflow, we want to introduce minimal changes to the code above to accomplish
the following:

* One or more of the functions should be visible and startable in the GUI
* The results of the nodes should be persisted in the database and visible in the GUI
* The nodes should be executable on remote resources

To achievieve this, we need to make the following changes in the code:

* decorate the functions that should be visible in the GUI with the `@node` decorator
* ensure that all input and output types of the nodes are registered odmantic Models
* specifiy where the nodes should be executed

For the specific example above the code looks as follows:

.. code-block:: python

    @node(parameters=Parameters(resource="my-remote-host",queue="slurm-queue"))
    def adder(node_input: BinaryOperationInput) -> FloatData:
        return FloatData(value=node_input.arg1 + node_input.arg2)

    @node(parameters=Parameters(resource="my-remote-host",queue="default"))
    def multiplier(node_input: BinaryOperationInput) -> FloatData:
        return FloatData(value=node_input.arg1 * node_input.arg2)

    @node
    def add_multiply_python(node_input: AddMultiplyInput) -> FloatData:
        add_result = adder(BinaryOperationInput(arg1=node_input.a, arg2=node_input.b))
        multiply_result = multiplier(BinaryOperationInput(arg1=add_result.value, arg2=node_input.c))
        return FloatData(value=multiply_result)


This would result in a workflow where all nodes are visible in the GUI (:ref:`submitting-workflows`).
The nodes `adder` and `multiplier` would be executed on a runner running on "my-remote-host".
The adder would be submitted to the "slurm-queue" and the multiplier would be executed immediately in the foreground.
add_multiply_python would be executed on the default resource by a runner called `local`

Because there is an intricate relationship between the class structure in python, the tables in the database
and the representation of this data in the GUI, these models are discussed in :ref:`persisting-results-section`.

Here are the ground rules to write workflows in Simstack II:

.. important::
  - Node functions must have a **unique name** (across the whole installation).
  - Node functions must have the **\*\*kwargs argument**
  - Node functions must **pass** the \*\*kwargs argument to nodes they call
  - Node functions must be **registered in the node_model table** of the database
    by running make_node_table (:ref:`registering`)
  - All positional arguments of node functions must be **registered Models** in the model_table of the database
    by running make_model_table (:ref:`registering`). Presently kwargs for models are not supported.

Beyond that nodes functions are normal python functions which can use the full flexibility of python.
The can call other functions or other nodes, but should **avoid "closures"**, such as global variables.
The @node decorator embeds the simple python functions into a complex workflow class
(see: :func:`simstack.core.node.node` in module: :mod:`simstack.core.node`).
You can consult the documentation of this class to understand how this works but this is not required to write
workflows.

The choice, whether a function is a "normal" function or a node is influenced
by the following considerations:

- Only nodes are visible in the GUI
- only nodes can be executed automatically on remote resources
- in principle nodes can call normal functions which can call nodes, as long as the \*\*kwargs
  are passed correctly

Return Types
~~~~~~~~~~~~

In workflows, the results of node functions must be visible in the databse and may be
processed by other nodes. This means that **results** of the nodes must by odmantic models which
can be visualized in the GUI and passed to other nodes which may execute on different resources.

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
        node_runner.info_files.append(some_file_with_log_output)

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

In order to keep track of the node execution, the Simstack core modules and
user written nodes should write logs. Simstack will initialize a custom
database logger which will be added to the logging system. In your node you
can therefore just instantiate and use a logger

.. code-block:: python

    import logging
    logger = logging.getLogger("name")

    @node
    def my_node(*args,**kwargs) -> SimstackResult

        node_runner = kwargs['node_runner'] # should never fail

        # this will log to the database and be visible in the general logs
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
  For odmantic models which have a ``complex_hash_function`` this function will
  override the default behavior. Odmantic ObjectId are not hashed.
- if a NodeRegistry :class:`simstack.models.node_registry.NodeRegistry` entry
  matching the name of the function, the hash of the
  arguments and the hash of the function is found, this registry entry is
  returned, even if the node has failed unless force_rerun is True.
- if no such NodeRegistry entry exists a new one is created with TaskStatus.SUBMITTED
- the node class then calls the function :func:`simstack.core.node.Node.run_somewhere`
  which executes the node locally using
  :func:`simstack.core.node.Node.execute_node_locally` or waits until
  the task status is changed in the database by a runner. This function will
  set the ``status`` field in the ``node_registry`` entry depending on the
  outcome of the node execution an process the data in ``node_runner``
- after node execution the results from remote nodes are loaded from the
  database.

.. note::
  Timeouts for remote execution are not yet implemented.

Remote Execution
~~~~~~~~~~~~~~~~

Nodes scheduled to run on a remote execution require a runner
(:func:`simstack.core.runner.runner`) to be running on that resource with the
--resource flag set to the name of that node. The user or the administrator
has to install Simstack and the relevant user repositories on the remote
server and specify the information relevant to the remote node in the
``simistack.toml`` configuration file (see: :ref:`configuration-file`). This
needs to be done only once.

When someone is actively developing new nodes or other code to be run on the
remote node, it is important to keep the code on the remote server up-to-date.
The preferred way is to periodically check for a git-update or other server
crashes and restart the server if such events are detected.
The file :file:`simstack.scripts.check_runner.sh` provides a template that
must be adapted to the configuration of the remote node. This needs to be
done only once.

.. _registering:

Registering Models and Nodes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Database access to the mongodb database in Simstack II is facilitated by a
using the odmantic package, which provides asynchronous object oriented access
to the database (see :ref:`persisting_data`). This means that data in the
generic code-base of Simstack is loaded/stored by storing class instances.

These classes cannot be imported via the standard ``import`` statements,
because they are unknown to the SimStack core modules. To overcome this
problem a dynamic import system has been implemented, where the module paths
of Simstack Models and Nodes are stored in database tables (``node_model``
and ``model_table``, respectively).

The functions :func:`simstack.core.model_table.make_model_table` and
:func:`simstack.core.node_table.make_node_table` scan the directories
provided in the path section of the configuration file
(:ref:`configuration_file`)


.. code-block:: python

    [paths]
    # Path configuration for the PathManager.
    # Each path entry should have a path and an optional drops value.
    # The path is the directory to search for Python files
    # The "drops" value is a prefix to drop from module names (for import paths)
    models = { path = "src\\simstack\\models", drops = "src" }
    methods = { path = "src\\simstack\\methods", drops = "src" }
    ui_testing = { path = "src\\simstack\\ui_testing", drops = "src" }
    applications = { path = "applications", drops = "", use_pickle = false }
    tests = { path = "tests", drops = "", use_pickle = false }
    projects = { path = "projects", drops = "",
                         use_pickle = false }

The first 4 entries are standard paths. The first three (models, methods,
ui_testing) belong to the Simstack core. ``applications`` is the standard
directory where you should install community application packages for
specific application domains.

The nodes and application programs written by you should go under
``projects``.

Most Common Errors & Problems
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* remote runners are not on the same branch or not up-to-date (see Health
  Check in the GUI)
* node or model not registered in the database (see :ref:`registering`)
* node or model not imported correctly (see below)
* forgetting \*\*kwargs in the node function definition. Nodes will crash.
* relative imports suggested by IDE:

.. code-block:: python

    from models import Model
    from core.nodes import node


make sure all imports are relative to simstack. For example:

.. code-block:: python

    from simstack.core.parameters import Parameters
    from simstack.core.workflow import Workflow
    from simstack.core.runner import Runner

or relative the other top-level modules. e.g. applications
