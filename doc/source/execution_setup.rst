Resource-Specific Setup and Program Configuration
===================================================

This document explains how SimStack manages resource-specific execution environments and program settings using the configuration file, Jinja2 templates, and the Python runner infrastructure.

Config.toml Structure
---------------------

The `config.toml` file at the project root is the central point for defining execution resources and specific program configurations. It is structured into resource-specific sections, each containing setup, post-processing, and program-specific details.

### Resource Section

Each top-level key in `config.toml` represents an execution resource (e.g., `local`, `justus`, `horeka`).

*   **`os`**: (Optional) The target operating system (`"linux"` or `"windows"`). Defaults to `"linux"`.
*   **`setup`**: Contains initial environment setup.
    *   **`scripts`**: A list of shell commands to run at the start of the script.
    *   **`tmp_base_dir`**: A shell command or block that sets the `TMP_BASE_DIR` environment variable. This is used for scratch space management.
*   **`post-processing`**:
    *   **`scratch_cleanup`**: (Boolean) Whether to delete the temporary workspace after execution.

### Program Section

Within a resource, you can define multiple program configurations under `[resource.program.program_name]`.

*   **`use_tmp`**: (Boolean, default `true`) Whether to use a temporary scratch directory.
*   **`environment_modules`**: A list of software modules to load (e.g., `["orca/6.1.1"]`).
*   **`program_env`**: A dictionary of environment variables to set specifically for this program.
*   **`input_files`**: Files to be copied from the simulation directory to the scratch directory before execution.
*   **`output_files`**: Files to be copied back from the scratch directory to the simulation directory after execution.
*   **`run_command`**: The actual command to execute the program.
*   **`scripts`**: (Optional) Additional shell commands to run before the main command.

**Example `config.toml` fragment:**

.. code-block:: toml

    [justus]
    os = "linux"
    [justus.setup]
    scripts = ["export MKL_NUM_THREADS=1"]
    tmp_base_dir = 'TMP_BASE_DIR="${SCRATCH:-/tmp/$USER}"'

    [justus.program.orca]
    use_templates = true
    environment_modules = ["orca/6.1.1"]
    input_files = ["orca.inp"]
    output_files = ["orca.out"]
    run_command = "orca orca.inp > orca.out"

Templates
---------

SimStack uses Jinja2 templates located in `examples/templates` to generate shell scripts for different environments.

### Base Templates (`base_script.*.j2`)

The base templates (`base_script.sh.j2`, `base_script.ps1.j2`, `base_script.cmd.j2`) provide the boilerplate for resource initialization:
*   Standardizing environment variables like `USER`, `JOB_ID`, and `SIMSTACK_DIR`.
*   Executing resource-specific `setup.scripts`.
*   Setting up the `TMP_WORK_DIR` based on `tmp_base_dir`.
*   Handling cleanup logic after execution.

The base templates define several blocks that can be overridden: `setup`, `resource_setup`, `scratch_setup`, `run`, and `cleanup`.

### Program Templates (`generic_program.*.j2`)

The generic program templates extend the base templates. They override the `run` block to:
1.  Load `environment_modules`.
2.  Set `program_env` variables.
3.  Copy `input_files` to the scratch directory.
4.  Execute the `run_command`.
5.  Copy `output_files` back to the simulation directory.

Runner Infrastructure (`runner_templates.py`)
---------------------------------------------

The `simstack.core.runner_templates.py` module contains the logic to glue the configuration and templates together.

### `ExecutorTemplateManager`

This class is responsible for loading `config.toml` and managing the Jinja2 environment.
*   It automatically selects the correct base template based on the resource's `os` and `shell` settings.
*   It provides `render_script` and `render_from_file` methods to generate the final execution script.

### `ProgramExecutor`

The `ProgramExecutor` class is the high-level interface for creating execution scripts.
*   **Initialization**: It takes a `program_name` and a `resource`. It looks up the corresponding configuration in `config.toml`.
*   **Configuration Merging**: It merges the values from `config.toml` with any parameters passed during initialization (passed parameters take precedence).
*   **Rendering**: The `render()` method uses the `ExecutorTemplateManager` to produce the final shell script string.

**Usage Example:**

.. code-block:: python

    from simstack.core.runner_templates import ProgramExecutor

    # Initialize executor for ORCA on the 'justus' resource
    executor = ProgramExecutor(resource="justus", program_name="orca")

    # Generate the shell script
    script_content = executor.render()
    print(script_content)
