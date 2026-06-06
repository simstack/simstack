import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any

from bson import ObjectId
from simstack.core.context import context
from simstack.util.custom_model_dump import custom_model_dump
from simstack.util.importer import import_class

async def extract_node_data(node_id_str: str, target_dir_path: Path):
    # 1. Initialize context
    # We assume context is already initialized if this is run as a script in the right environment,
    # but for standalone execution we might need to initialize it.
    # If it's not initialized, initialize it with default settings.
    if not context.initialized:
        await context.initialize()

    # 2. Load NodeRegistry entry
    node_id = ObjectId(node_id_str)
    node_registry = await context.db.load_task_by_id(node_id)
    if not node_registry:
        print(f"Error: NodeRegistry with ID {node_id_str} not found.")
        return

    # 3. Create the target directory
    arg_hash = node_registry.arg_hash
    final_target_dir = target_dir_path / arg_hash
    final_target_dir.mkdir(parents=True, exist_ok=True)

    # 4. Copy files from context.workdir/node_name/id to target_dir/arg_hash
    # context.workdir returns a Path object
    source_dir = context.workdir / node_registry.name / node_id_str
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
    async def serialize_models(ids: List[ObjectId], tables: List[str]) -> List[Dict[str, Any]]:
        serialized = []
        for i, (oid, table) in enumerate(zip(ids, tables)):
            # table is the model mapping (e.g. "simstack.models.parameters.Parameters")
            model_cls = await import_class(table, context.db)
            if model_cls:
                model_inst = await context.db.find_one(model_cls, model_cls.id == oid)
                if model_inst:
                    data = await custom_model_dump(model_inst)
                    # The name might be available in input_names/result_names, but let's try to match them.
                    # This is a bit tricky if they don't match perfectly.
                    serialized.append({
                        "name": f"item_{i}", # Fallback name
                        "mapping": table,
                        "data": data
                    })
        return serialized

    inputs_serialized = await serialize_models(node_registry.input_ids, node_registry.input_tables)
    # Correct names if available
    if len(inputs_serialized) == len(node_registry.input_names):
        for item, name in zip(inputs_serialized, node_registry.input_names):
            item["name"] = name

    results_serialized = await serialize_models(node_registry.result_ids, node_registry.result_tables)
    if len(results_serialized) == len(node_registry.result_names):
        for item, name in zip(results_serialized, node_registry.result_names):
            item["name"] = name

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
