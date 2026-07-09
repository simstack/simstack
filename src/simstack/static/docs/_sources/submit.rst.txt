
Submitting New Workflows
------------------------

The Submit Panel is the main interface for creating and submitting computational jobs in the system. It provides a comprehensive workflow that guides users through model selection, parameter configuration, and input data specification.
To submit a new workflow:

1. Navigate to the submission page
2. Select the workflow type from the available options
3. Fill in the required parameters in the form
   - Forms are dynamically generated based on the workflow type
   - Required fields are marked with an asterisk (*)
   - Help text provides guidance for each parameter
4. Click "Submit" to start the workflow
5. You'll be redirected to the dashboard where you can monitor the task's progress

The submission forms use React JSON Schema Form, which provides:

* **Validation**: Immediate feedback on invalid inputs
* **Conditional Fields**: Fields that appear based on other selections
* **File Uploads**: For workflows that require input files
* **Advanced Controls**: Sliders, dropdowns, and other interactive elements
* **Special Fields**: for complex objects, e.g. molecular data

The Submit Panel consists of several key sections organized in a logical workflow:

1. **Model Selection** (CascadingNodeSelector)
2. **Job Configuration** (Custom Name & Category)
3. **Parameter Settings** (ParametersAccordion)
4. **Input Data Configuration** (Input Mappings)
5. **Template Management** (TemplateSelector)
6. **Job Submission**


Node Selector
~~~~~~~~~~~~~

The CascadingNodeSelector is the primary interface for browsing and selecting computational models from the available node registry.

.. figure:: resources/node_selector.png
   :alt: Node Selector Interface
   :align: center
   :width: 700px

   *Hierarchical model browser with favorites and tree structure*

**Hierarchical Model Organization**
   Models are organized in a tree structure based on their function mapping, making it easy to navigate through different categories and modules.

.. figure:: resources/model-tree-structure.png
   :alt: Model Tree Structure
   :align: center
   :width: 500px

   *Example of hierarchical model organization which mirrors the directory structure of your project*

**Favorites System**
   Users can mark frequently used models as favorites for quick access:

   - Star icon checkbox for marking/unmarking favorites
   - Dedicated "Favorites" section at the top of the tree
   - Persistent favorite status across sessions


Once a model is selected, the ParametersAccordion and Input sections dynamically update to reflect the chosen model's requirements.

Parameter Settings
~~~~~~~~~~~~~~~~~~

The ParametersAccordion section provides comprehensive job parameter configuration options.

.. figure:: resources/job-parameters.png
   :alt: Parameters Accordion
   :align: center
   :width: 700px

   *Complete parameters configuration interface (for a slurm-queue job)*

**Resource Selection**
   Configure computational resources for job execution:

   - Dropdown selection from allowed resources
   - Default option available if no specific resource needed
   - Dynamic loading of available resources from the system

**Queue Configuration**
   Specify execution queue for job scheduling:

   - Selection from allowed queues list
   - When the SLURM queue is selected, additional SLURM parameters become available

**SLURM Parameters** (when using "slurm-queue")
   Additional SLURM-specific configuration options become available when the SLURM queue is selected, including:

   - Job allocation parameters
   - Resource requirements
   - Time limits
   - Node specifications

**Execution Options**
   Control job execution behavior:

   - **Force Rerun**: Force execution even if cached results exist
   - **Recompute Artifacts**: Regenerate all output artifacts regardless of cache status

**Custom Name & Category Editor**
   Provides job identification and organization:

   **Custom Name Field**
      - User-defined name for the job
      - Defaults to "submitted-job"
      - Automatically saved to localStorage per operation

   **Category Field**
      - Organizational category for the job
      - Defaults to "submission"
      - Persistent storage with operation-specific recall

Model Input Section
~~~~~~~~~~~~~~~~~~~

The Input section handles data configuration for each input mapping required by the selected model.
For each input mapping defined by the selected model:

.. figure:: resources/sample-input.png
   :alt: Input Mapping Card
   :align: center
   :width: 650px

   *Individual input mapping configuration card*

**Dynamic Form Generation**
   - Automatically generated forms based on input mapping schema
   - Real-time validation and data binding
   - Memoized form components for optimal performance

**Template Management**
   - Load existing input templates
   - Save current configuration as template
   - Update existing templates on submission
   - Template-specific parameter inheritance
