import argparse
import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List

from odmantic import Model, ObjectId
from pydantic import BaseModel

from simstack.core.context import context
from simstack.models import NodeRegistry, NamedDataReference
from simstack.util.importer import import_class

logger = logging.getLogger("generate_test")

async def load_models(references: List[NamedDataReference]) -> Dict[str, Any]:
    """Load models from the database based on references."""
    result = {}
    db = context.db
    for ref in references:
        try:
            model_cls = await import_class(ref.variable_mapping, db)
            if not model_cls:
                raise RuntimeError(f"Could not import class {ref.variable_mapping}")

            obj = await db.find_one(model_cls, model_cls.id == ref.reference)
            if obj is None:
                raise RuntimeError(
                    f"Object {ref.reference} not found in {ref.variable_mapping}"
                )

            # Preserve the argument/result name captured at execution time.
            key = ref.variable_name
            if not key:
                from simstack.models import ModelMapping

                model_mapping = await db.find_one(
                    ModelMapping,
                    ModelMapping.mapping == ref.variable_mapping,
                )
                key = model_mapping.name if model_mapping else ref.variable_mapping

            if key in result:
                key = f"{key}_{str(ref.reference)}"
            result[key] = obj
        except Exception as e:
            logger.exception(f"Error loading {ref.variable_mapping} {ref.reference}: {e}")
            raise RuntimeError(
                f"Failed to load {ref.variable_mapping} {ref.reference}"
            ) from e
    return result

def serialize_models(models: Dict[str, Any]) -> str:
    """Serialize models to a JSON string."""
    data = {}
    for key, obj in models.items():
        if isinstance(obj, (Model, BaseModel)):
            data[key] = obj.model_dump()
        else:
            data[key] = str(obj)
    return json.dumps(data, indent=4, default=str)

async def generate_test(node_id: str, target_base: Path):
    """Generate a test case for a given node ID."""
    db = context.db
    registry_entry = await db.find_one(NodeRegistry, NodeRegistry.id == ObjectId(node_id))
    if not registry_entry:
        raise ValueError(f"NodeRegistry entry with ID {node_id} not found.")

    node_name = registry_entry.name
    arg_hash = registry_entry.arg_hash
    
    # Target directory: target/node_name/arg_hash
    target_dir = target_base / node_name / arg_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Source directory: workdir/node_name/id
    workdir = context.config.workdir
    source_dir = workdir / node_name / str(registry_entry.id)
    
    if source_dir.exists() and source_dir.is_dir():
        print(f"Copying files from {source_dir} to {target_dir}")
        # Copy all files from source to target
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, target_dir / item.name)
            elif item.is_dir():
                shutil.copytree(item, target_dir / item.name, dirs_exist_ok=True)
    else:
        print(f"Source directory {source_dir} does not exist. Skipping file copy.")

    # Load and serialize inputs
    print(f"Serializing inputs for {node_id}")
    inputs = await load_models(registry_entry.input_references)
    with open(target_dir / "inputs.json", "w") as f:
        f.write(serialize_models(inputs))

    # Load and serialize outputs
    print(f"Serializing outputs for {node_id}")
    outputs = await load_models(registry_entry.results_references)
    with open(target_dir / "outputs.json", "w") as f:
        f.write(serialize_models(outputs))

    print(f"Test generation for node {node_id} completed at {target_dir}")

async def async_main():
    parser = argparse.ArgumentParser(description="Generate a test case from a node execution.")
    parser.add_argument("--id", required=True, help="ID of the NodeRegistry entry")
    parser.add_argument("--target", default=str(Path.cwd() / "tests"), help="Target base directory for tests")
    parser.add_argument("--resource", default="self", help="Resource to use for database connection")
    
    args = parser.parse_args()
    
    await context.initialize(resource=args.resource)
    
    await generate_test(args.id, Path(args.target))

def generate_test_main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(async_main())

if __name__ == "__main__":
    generate_test_main()
