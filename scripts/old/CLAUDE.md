# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Using Pixi (Recommended)
- **Install dependencies**: `pixi install`
- **Run tests**: `pixi run tests` (equivalent to `python -m pytest --junitxml=pytest.xml --cov-report=term-missing:skip-covered --cov=simstack`)
- **Run specific tests**: `pixi run tests $PYTESTOPTIONS` (where `$PYTESTOPTIONS` can be any pytest arguments)
- **Run RDKit tests**: `pixi run rdkit-tests` (runs RDKit-specific tests in rdkit environment)
- **Run single test**: `pixi run -e test python -m pytest tests/path/to/test_file.py::test_function`
- **Type checking**: `pixi run mypy` (runs `mypy .`)
- **Linting**: `pixi run lint` (runs pre-commit hooks)
- **Pre-commit setup**: `pixi run pre-commit-install`

### Alternative (pip-based)
- **Install dependencies**: `pip install -r requirements.txt`
- **Run tests**: `python -m pytest`
- **Single test**: `python -m pytest tests/path/to/test_file.py::test_function -v`

### Building
- **Python build**: `pixi run pythonbuild` (equivalent to `python3 -m build`)
- **Conda build**: `pixi run condabuild` (equivalent to `conda build conda-recipe/`)

## High-Level Architecture

### Core Components
- **Node System**: The heart of SimStack II - computational nodes decorated with `@node` that define reusable workflow components
- **Data Models**: Pydantic-based models using `@simstack_model` decorator for serialization, UI schema generation, and database persistence
- **Runner System**: Distributed task execution across local and remote resources (HPC clusters) with SLURM integration
- **Context Management**: Global configuration and database engine management through `simstack.core.context`

### Key Directories
- `src/simstack/core/`: Core workflow engine (node.py, runner.py, engine.py, context.py)
- `src/simstack/models/`: Data models and schemas (simstack_model.py, models.py, parameters.py)
- `src/simstack/server/`: FastAPI web server and REST API routes
- `applications/electronic_structure/`: Domain-specific quantum chemistry nodes (Gaussian, ORCA, Turbomole)
- `examples/`: Workflow examples demonstrating the node system
- `tests/`: Unit and integration tests

### Configuration
- **Main config**: `simstack.toml` (defines resources, MongoDB connection, host mappings)
- **Template**: `simstack_template.toml` provides configuration example
- **Python path**: Must include both project root and `src/` directory

### Database Integration
- Uses **MongoDB** with **Odmantic** ODM for model persistence
- All `@simstack_model` decorated classes are automatically stored/retrieved
- Task status and results tracked in database collections

### Testing Framework
- **pytest** with async support (`asyncio_mode = auto`)
- Test markers: `slow`, `integration`, `unit`, `local_runner`, `database`
- Coverage reporting enabled
- Skip slow tests: `pytest -m "not slow"`

### Workflow Pattern
1. Define input/output models using `@simstack_model`
2. Create computational functions decorated with `@node`
3. Chain nodes together to form workflows
4. Execute locally or submit to remote HPC resources via runners
5. Results automatically persisted to MongoDB

### Remote Execution
- Runners connect to MongoDB message broker
- SLURM job scripts auto-generated for HPC submission
- File transfer between local/remote resources handled transparently
- Support for multiple HPC systems (configured in simstack.toml)
