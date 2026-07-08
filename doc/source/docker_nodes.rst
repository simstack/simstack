.. _docker_nodes:

Creating Docker Containers for SimStack Nodes
=============================================

This document explains the mechanism for creating Docker containers for SimStack nodes, using the `psi4_node` as a practical example.

Mechanism Overview
------------------

SimStack nodes can be containerized to ensure a consistent execution environment, especially when complex dependencies like quantum chemistry codes (e.g., Psi4) are involved. 

The general approach involves:
1. Creating a `Dockerfile` that sets up the environment and installs necessary software.
2. Including SimStack and its core running mechanism in the container.
3. Defining an `ENTRYPOINT` that calls the SimStack node runner.

Example: Psi4 Node
------------------

The `psi4_node` is part of the `molecular_qm_psi4` package. It requires `psi4` and `crest` which are best installed via `micromamba` (a fast alternative to conda).

Node Implementation
^^^^^^^^^^^^^^^^^^^

The node is defined in `molecular_qm_psi4/psi4_node.py`. It uses the `@node` decorator and interacts with SimStack's `node_runner`.

.. code-block:: python

    import logging
    try:
        import psi4
    except ImportError:
        psi4 = None

    from simstack.core.node import node
    from simstack.core.simstack_result import SimstackResult
    from molecular_qm_models import QMInput, QMResult, Molecule, Atom, MoleculeList

    logger = logging.getLogger(__name__)

    def qminput_to_psi4_molecule(molecule: Molecule, charge: int, multiplicity: int) -> str:
        """Converts a Simstack Molecule to a Psi4 molecule string."""
        mol_str = f"{charge} {multiplicity}\n"
        for atom in molecule.atoms:
            mol_str += f"{atom.element} {atom.x} {atom.y} {atom.z}\n"
        return mol_str

    @node
    async def psi4_calculator(qm_input: QMInput, **kwargs) -> SimstackResult:
        """
        Psi4 node implementation using Python bindings.
        """
        node_runner = kwargs.get("node_runner")
        
        if psi4 is None:
            return node_runner.fail("Psi4 is not installed in the current environment.")

        try:
            # Set up Psi4 molecule
            mol_str = qminput_to_psi4_molecule(qm_input.molecule, qm_input.charge, qm_input.multiplicity)
            psi4.geometry(mol_str)
            
            # ... (configuration and execution logic) ...
            
            # Execute calculation
            if qm_input.optimization:
                energy, wfn = psi4.optimize(method, return_wfn=True)
                # ...
            else:
                energy, wfn = psi4.energy(method, return_wfn=True)
                
            qm_result = QMResult()
            qm_result.final_energy = energy
            # ...
            
            node_runner.info("Psi4 calculation finished successfully")
            node_runner.psi4_result = qm_result
            return node_runner.succeed()

        except Exception as e:
            logger.error(f"Psi4 calculation failed: {str(e)}")
            return node_runner.fail(f"Psi4 execution failed: {str(e)}")
        finally:
            if psi4 is not None:
                psi4.core.clean()

Dockerfile Configuration
^^^^^^^^^^^^^^^^^^^^^^^^

The `Dockerfile` for this node is located at `molecular_qm_psi4/Dockerfile`. It uses `micromamba` for environment management and `uv` for fast Python package installation.

.. code-block:: dockerfile

    # Use a base image with micromamba (conda replacement)
    FROM mambaorg/micromamba:latest

    USER root

    # Install git and other build essentials
    RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        build-essential \
        && rm -rf /var/lib/apt/lists/*

    # Fix for _distutils_hack error: explicitly install setuptools
    RUN micromamba install -y -n base -c conda-forge setuptools && \
        micromamba clean --all --yes

    # Install uv
    RUN curl -LsSf https://astral.sh/uv/install.sh | sh
    ENV PATH="/opt/conda/bin:/root/.local/bin:$PATH"

    # Set up work directory
    WORKDIR /app

    # Install Psi4 and crest from conda
    RUN micromamba install -y -n base -c conda-forge psi4 crest python=3.12 && \
        micromamba clean --all --yes

    # Copy the repositories/directories
    COPY molecular_qm_models /app/molecular_qm_models
    COPY molecular_qm_psi4 /app/molecular_qm_psi4
    COPY molecular_qm_psi4/README.md /app/README.md
    COPY simstack.toml /app/simstack.toml

    ENV PYTHONPATH="/app:/app/molecular_qm_psi4"
    ENV UV_PYTHON=/opt/conda/bin/python
    ENV UV_PROJECT_ENVIRONMENT=/opt/conda
    ENV PATH="/opt/conda/bin:/root/.local/bin:$PATH"

    # Install dependencies directly
    RUN uv pip install --system "simstack @ git+https://github.com/simstack/simstack.git@feature-test-generation" "setuptools>=80.9.0"

    # Entrypoint: call the module directly
    ENTRYPOINT ["python", "-m", "simstack.core.run_node"]

Key Components for Containerization
-----------------------------------

1. **Base Image**: Use a lightweight and appropriate base image (e.g., `micromamba` for scientific software).
2. **Environment Setup**: Install all non-Python dependencies (compilers, libraries, etc.).
3. **Python Environment**: Install Python and required packages. The use of `uv` is recommended for performance.
4. **Code Integration**: Copy your node code and its local dependencies into the container.
5. **PYTHONPATH**: Ensure that the `PYTHONPATH` is set correctly so that SimStack can find your node and models.
6. **SimStack Installation**: Install the `simstack` package itself.
7. **Entrypoint**: The `ENTRYPOINT ["python", "-m", "simstack.core.run_node"]` is crucial as it tells SimStack how to execute the node within the container.
