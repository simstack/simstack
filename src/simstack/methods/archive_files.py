import asyncio
import inspect
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Callable, Any, Optional

from docutils.nodes import field_name
from odmantic import Model, Field

from simstack.core.context import context
from simstack.core.node import node
from simstack.models import FileStack, FileList, simstack_model
from simstack.models.base_lists import BooleanDataList
from simstack.models.file_instance import FileInstance


def archive_one_file(file_stack: FileStack,**kwargs):
    node_runner = kwargs["node_runner"]
    node_runner.info(f"Archiving file: {file_stack.name}")

    config = context.resource_config.get_program("archive_one_file")
    if config.get("archive_path", None) is None:
        raise ValueError("archive_path not set in config")
    archive_path = Path(config.get("archive_path"))
    full_path = archive_path / file_stack.id
    full_path.mkdir(parents=True, exist_ok=True)
    if full_path.exists() and full_path.is_dir():
        try:
            archive_location = file_stack.get(local_dir=full_path)
            archive_file_instance = FileInstance.from_local_file(archive_location)
            node_runner.info(f"Archived file to {archive_file_instance.path}")
            file_stack.locations.append(archive_file_instance)
            return True
        except Exception as e:
            node_runner.error(f"Error archiving file: {str(e)}")
            return False
    else:
        raise ValueError(f"Archive path {archive_path} does not exist or is not a directory")

@node
async def archive_file(file_stack: FileStack,**kwargs):
    return archive_one_file(file_stack, **kwargs)

@node
def archive_files(file_list: FileList,**kwargs):
    archive_results = BooleanDataList(field_name="archive_result")
    for file_stack in file_list:
        archive_results.append(archive_one_file(file_stack, **kwargs))
    return archive_results


@simstack_model
class ArchiveConfig(Model):
    call_paths: List[str] = Field(default_factory=list)
    archive_resource: str = Field(default="archive_resource", description="Name of the resource where the files will be archived")
    in_memory: bool = Field(default=False, description="Whether to archive files in memory")
    use_time_window: bool = Field(default=False, description="Whether to archive files within a time window")
    start_date: Optional[datetime] = Field(default=datetime.now(), description="Start of the period to archive files for")
    end_date: Optional[datetime] = Field(default=datetime.now(), description="End end of the period to archive files for")
    include_patterns: List[str] = Field(default_factory=list, description="List of file patterns to include")
    exclude_patterns: List[str] = Field(default_factory=list, description="List of file patterns to exclude")
    min_size: int = Field(default=0, description="Minimum size of files to archive")
    filter_by_resource: bool = Field(default=False, description="Whether to only archive files that have a location on the current resource")


@node
async def archive_node(archive_config: ArchiveConfig, **kwargs):
    """
    Archives FileStacks matching the criteria in archive_config and deletes local instances upon success.

    Args:
        archive_config (ArchiveConfig): Configuration for the archive process.
        **kwargs: Additional arguments.
    """
    node_runner = kwargs["node_runner"]
    db = context.db
    my_resource = context.config.resource
    
    # Discover FileStacks
    all_file_stacks = []
    if archive_config.call_paths:
        from simstack.models import NodeRegistry, FileListModel

        # Find matching NodeRegistry entries
        node_query = {
            "call_path": {"$in": archive_config.call_paths},
            "parameters.resource.value": my_resource
        }
        matching_nodes = await db.find(NodeRegistry, node_query)
        
        # We also need mappings to know which collection to look into
        # context.model_mappings has by_mapping and by_name
        filestack_mapping = context.model_mappings.get_by_name("FileStack")
        filelist_mapping = context.model_mappings.get_by_name("FileList")
        filelistmodel_mapping = context.model_mappings.get_by_name("FileListModel")
        
        fs_mapping_str = filestack_mapping.mapping if filestack_mapping else ""
        fl_mapping_str = filelist_mapping.mapping if filelist_mapping else ""
        flm_mapping_str = filelistmodel_mapping.mapping if filelistmodel_mapping else ""

        found_fs_ids = set()
        found_fl_ids = set()
        found_flm_ids = set()

        for node in matching_nodes:
            # Check info_files
            for fs in node.info_files:
                found_fs_ids.add(fs.id)
            
            # Check input_references and results_references
            for ref in node.input_references + node.results_references:
                if ref.variable_mapping == fs_mapping_str:
                    found_fs_ids.add(ref.reference)
                elif ref.variable_mapping == fl_mapping_str:
                    found_fl_ids.add(ref.reference)
                elif ref.variable_mapping == flm_mapping_str:
                    found_flm_ids.add(ref.reference)

        # Retrieve FileStacks from found IDs
        if found_fs_ids:
            file_stacks = await db.find(FileStack, {"_id": {"$in": list(found_fs_ids)}})
            all_file_stacks.extend(file_stacks)
        
        # Retrieve FileLists and their FileStacks
        if found_flm_ids:
            file_list_models = await db.find(FileListModel, {"_id": {"$in": list(found_flm_ids)}})
            for flm in file_list_models:
                # FileListModel uses ObjectListMixin[FileStack], elements are ObjectIds
                # We need to fetch the actual FileStack objects.
                if flm.elements:
                    stacks = await db.find(FileStack, {"_id": {"$in": flm.elements}})
                    all_file_stacks.extend(stacks)

        # Deduplicate all_file_stacks by ID
        unique_stacks = {}
        for fs in all_file_stacks:
            unique_stacks[fs.id] = fs
        all_file_stacks = list(unique_stacks.values())

    else:
        # Existing logic: Query FileStack collection directly
        query = {}
        if archive_config.min_size > 0:
            query["size"] = {"$gte": archive_config.min_size}

        if archive_config.filter_by_resource:
            query["locations.resource.value"] = my_resource

        all_file_stacks = await db.find(FileStack, query)
    filtered_file_stacks = []

    for fs in all_file_stacks:
        # Filter by date if specified
        # Since FileStack doesn't have a date, we check its locations.
        if archive_config.start_date or archive_config.end_date:
            valid_date = False
            for loc in fs.locations:
                if loc.created_at:
                    if archive_config.start_date and loc.created_at < archive_config.start_date:
                        continue
                    if archive_config.end_date and loc.created_at > archive_config.end_date:
                        continue
                    valid_date = True
                    break
            if not valid_date:
                continue

        # Filter by patterns
        if archive_config.include_patterns:
            import fnmatch
            if not any(fnmatch.fnmatch(fs.name, pat) for pat in archive_config.include_patterns):
                continue

        if archive_config.exclude_patterns:
            import fnmatch
            if any(fnmatch.fnmatch(fs.name, pat) for pat in archive_config.exclude_patterns):
                continue

        filtered_file_stacks.append(fs)

    if not filtered_file_stacks:
        node_runner.info("No files found matching the archival criteria.")
        return BooleanDataList(field_name="archive_results")

    # 2. Add them to FileList
    archive_file_list = FileList(field_name="archive_files")
    for fs in filtered_file_stacks:
        archive_file_list.append(fs)

    # 3. Call archive_files on the filelist
    # Note: archive_files is a @node, we can call it directly.
    # It returns a BooleanDataList
  
    # To handle mocks and different return types, we ensure we get a BooleanDataList
    # When calling a @node decorated function, it returns a SimstackResult or Model in sync mode,
    # or the result of the function if it was run locally.
    # If we are in the same process and calling it, it might be running via NodeRunner.
    raw_results = await archive_files(archive_file_list, **kwargs)
    print(f"DEBUG: raw_results type={type(raw_results)}")
    if isinstance(raw_results, BooleanDataList):
        archive_results = raw_results
    else:
         # If mocked or returned as list/iterable
         archive_results = BooleanDataList(field_name="archive_results")
         from simstack.models import BooleanData
         if raw_results is not None:
             # Handle possible async result if not awaited properly by decorator
             if inspect.isawaitable(raw_results):
                 raw_results = await raw_results
             
             for r in raw_results:
                 if isinstance(r, bool):
                     archive_results.append(BooleanData(value=r))
                 elif isinstance(r, BooleanData):
                     archive_results.append(r)
                 else:
                     # try to save it if it's a model
                     archive_results.append(r)

    # 4. Delete local FileInstances for successfull archival
    # archive_results is a BooleanDataList, elements are BooleanData (presumably)
    # Wait, archive_files returns BooleanDataList. Let's check BooleanDataList implementation.
    
    # Actually archive_files returns BooleanDataList where each element corresponds to a FileStack in archive_file_list
    for i, success_data in enumerate(archive_results):
        if success_data.value:
            fs = archive_file_list[i]
            # Delete local FileInstances
            # We need to know which ones are "local".
            # Usually those are on the current resource.
            current_resource = context.config.resource
            
            remaining_locations = []
            for loc in fs.locations:
                # If it's on the current resource and is a local path, delete it
                if loc.resource.value == current_resource and loc.location_type == "local_path":
                    try:
                        local_path = Path(context.config.workdir) / loc.path
                        if local_path.exists():
                            if local_path.is_file():
                                local_path.unlink()
                            elif local_path.is_dir():
                                shutil.rmtree(local_path)
                            node_runner.info(f"Deleted local instance of {fs.name} at {local_path}")
                        else:
                            node_runner.warning(f"Local path {local_path} for {fs.name} does not exist")
                    except Exception as e:
                        node_runner.error(f"Failed to delete local instance of {fs.name}: {e}")
                        remaining_locations.append(loc)
                else:
                    remaining_locations.append(loc)
            
            fs.locations = remaining_locations
            await db.save(fs)

    return archive_results

