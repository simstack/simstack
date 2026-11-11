import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any

from simstack.models.document import ObjectId
from simstack.core.context import context
from simstack.util.custom_model_dump import custom_model_dump
from simstack.server_api.http_transport import RunnerAPIError

async def extract_node_data(node_id_str: str, target_dir_path: Path):
    # 1. Initialize context
    # We assume context is already initialized if this is run as a script in the right environment,
    # but for standalone execution we might need to initialize it.
    # If it's not initialized, initialize it with default settings.
    if not context.initialized:
        await context.initialize()

    # 2. Load NodeRegistry entry
    node_id = ObjectId(node_id_str)
    node_registry = None
    try:
        node_registry = await context.server.tasks.get_task(node_id)
    except RunnerAPIError as exc:
        if exc.status_code != 404:
            raise
    if not node_registry:
        print(f"Error: NodeRegistry with ID {node_id_str} not found.")
        return

    # 3. Create the target directory
    arg_hash = node_registry.arg_hash
    final_target_dir = target_dir_path / arg_hash
    final_target_dir.mkdir(parents=True, exist_ok=True)

    # 4. Copy files from the task workdir to its exported snapshot.
    source_dir = context.config.workdir / node_registry.name / node_id_str
    if source_dir.exists() and source_dir.is_dir():
        for item in source_dir.iterdir():
            if item.is_dir():
                shutil.copytree(item, final_target_dir / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, final_target_dir / item.name)
        print(f"Copied files from {source_dir} to {final_target_dir}")
    else:
        print(f"Warning: Source directory {source_dir} does not exist or is not a directory.")

    # 5. Serialize inputs and results
    async def serialize_references(references: List[Any], *, inputs=False) -> List[Dict[str, Any]]:
        serialized = []
        values = (await context.server.values.resolve_task_inputs(node_registry.id) if inputs
                  else await context.server.values.resolve_task_outputs(node_registry))
        for ref, model_inst in zip(references, values):
            data = await custom_model_dump(model_inst)
            serialized.append({
                "name": ref.variable_name,
                "mapping": ref.variable_mapping,
                "data": data
            })
        return serialized

    inputs_serialized = await serialize_references(node_registry.input_references, inputs=True)
    results_serialized = await serialize_references(node_registry.results_references)

    with open(final_target_dir / "inputs.json", "w") as f:
        json.dump(inputs_serialized, f, indent=4, default=str)
    
    with open(final_target_dir / "results.json", "w") as f:
        json.dump(results_serialized, f, indent=4, default=str)

    # 6. Serialize NodeRegistry to id.json
    node_registry_data = await custom_model_dump(node_registry)
    with open(final_target_dir / f"{node_id_str}.json", "w") as f:
        json.dump(node_registry_data, f, indent=4, default=str)

    print(f"Extraction completed for node {node_id_str}. Data saved in {final_target_dir}")

def main():
    parser = argparse.ArgumentParser(description="Extract node data and copy files.")
    parser.add_argument("--id", required=True, help="NodeRegistry ID")
    parser.add_argument("--target", default=str(Path.cwd() / "tests" / "data"), help="Target directory (defaults to cwd/tests/data)")
    
    args = parser.parse_args()
    
    target_path = Path(args.target)
    
    asyncio.run(extract_node_data(args.id, target_path))

if __name__ == "__main__":
    main()
