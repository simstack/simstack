from datetime import datetime

from simstack.core.node import node
from simstack.models.file_instance import FileInstance
from simstack.models.files import FileGetterArgs, FileStack
from simstack.util.file_transfer_client import (
    path_for_file_instance,
    resolve_instance_path,
    transfer_resource_name,
)


@node
def get_file(file_getter_args: FileGetterArgs, **kwargs) -> FileStack:
    """Materialize a FileStack in the requested directory on the executing resource."""
    from simstack.core.context import context

    resource = transfer_resource_name(file_getter_args.local_resource)
    current = transfer_resource_name(context.config.resource)
    if resource != current:
        raise ValueError(f"Requested resource {resource!r}, but this node runs on {current!r}")

    stack = file_getter_args.file_stack
    path = stack.get_raw(
        file_getter_args.local_resource, file_getter_args.local_dir, copy_local_source=True
    )
    workdir = context.config.workdir
    target = path.resolve()
    if not any(
        loc.location_type == "local_path" and loc.status == "available"
        and transfer_resource_name(loc.resource) == resource
        and resolve_instance_path(loc.path, workdir).resolve() == target
        for loc in stack.locations
    ):
        stack.append(FileInstance(
            path=path_for_file_instance(target, workdir),
            resource=resource,
            created_at=datetime.now(),
            size_bytes=target.stat().st_size,
        ))
    return stack
