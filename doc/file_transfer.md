# SimStack File Transfer System

This document provides an overview of the SimStack File Transfer System, which allows for efficient file transfers between different computing resources.

## Overview

The SimStack File Transfer System is designed to manage the transfer of files between different computing resources in a distributed environment. It provides a high-level abstraction for file management, handling both local and remote file operations, with support for in-memory storage and remote file retrieval.

## Key Components

### FileInstance

`FileInstance` represents a file at a specific location (resource). A file can exist on multiple resources simultaneously.

Key attributes:
- `path`: Path to the file relative to the resource's working directory
- `resource`: Name of the resource where the file is located
- `in_memory`: Boolean indicating if the file content is stored in memory
- `content`: Compressed file content when stored in memory
- `is_hashable`: Whether the file has a hash for content verification
- `hash`: Hash value for content verification
- `created_at`: Timestamp of when the file instance was created
- `file_stack_id`: Reference to the parent FileStack

Methods:
- `transfer_to(target_resource, target_path)`: Transfer the file to another resource

### FileStack

`FileStack` represents a collection of `FileInstance` objects for the same logical file across different resources. It manages the different locations of a file and provides methods to retrieve the file to the local system.

Key functionality:
- Tracking instances across multiple resources
- Retrieving a file to the local system using the best available method
- Handling remote file retrieval through job orchestration
- Transferring files between resources

Methods:
- `get(local_dir)`: Retrieve the file to a local directory
- `transfer_to_resource(target_resource, local_dir)`: Transfer the file to a target resource

### @copy Decorator

The `@copy` decorator is a powerful utility for simplifying file transfers between resources. It wraps functions that provide file transfer information and handles the actual transfer process.

Key features:
- Orchestrates file transfers according to defined routes
- Handles job submission and monitoring
- Provides a consistent interface for various transfer scenarios

The decorated function should return a tuple of:
- `source_resource`: The resource where the file is currently located
- `source_path`: The path to the file on the source resource
- `target_resource`: The resource where the file should be transferred to
- `target_path`: The path where the file should be placed on the target resource

### Route Finding

The route finding system determines the best path to transfer a file from one resource to another.

Key components:
- `find_minimal_route`: Finds the shortest path between resources based on defined routes
- `find_shortest_route`: Alias for compatibility with existing code

Routes are defined in the SimStack configuration file (`simstack.toml`) and specify:
- The source resource
- The target resource
- The host responsible for executing the transfer

## File Retrieval Process

When retrieving a file using `FileStack.get()`, the system follows this process:

1. **Check for local instances**:
   - First, check for in-memory instances on the local resource
   - If not found, check for on-disk instances on the local resource

2. **Remote retrieval** (if no suitable local instance is found):
   - Identify remote resources with instances of the file
   - For each remote resource:
     - Find a route from the remote resource to the local resource
     - Submit a copy job to the appropriate executing host
     - Poll for job completion
     - Return the local file path once the transfer completes

3. **Error handling**:
   - If a transfer fails, the system tries the next available remote instance
   - If all transfers fail, an appropriate error is raised

## Configuration

Routes for file transfers are configured in the `simstack.toml` file:

```toml
# Example route configuration
[[routes]]
source = "local"
target = "remote1"
host = "local"

[[routes]]
source = "remote1"
target = "local"
host = "remote1"
```

Each route specifies:
- `source`: The resource where the file currently exists
- `target`: The resource where the file should be transferred to
- `host`: The resource responsible for executing the transfer

## Examples

### Basic Usage

```python
from simstack.models.files import FileStack
from simstack.models.file_instance import FileInstance

# Create a FileStack from a local file
file_stack = FileStack.from_local_file("/path/to/local/file.txt")

# Retrieve the file to a specific directory
output_path = file_stack.get(resource, local_dir="/output/directory")

# Transfer the file to a remote resource
file_stack.transfer_to_resource("remote_resource")
```

### Using the @copy Decorator

The `@copy` decorator can be applied to functions that provide file transfer information:

```python
from simstack.models.files import copy

@copy
def transfer_file(source_resource, target_resource, filename):
    source_path = f"/path/on/{source_resource}/{filename}"
    target_path = f"/path/on/{target_resource}/{filename}"
    
    # Return transfer information for the decorator to handle
    return (source_resource, source_path, target_resource, target_path)

# Call the decorated function to perform the transfer
result = transfer_file("local", "remote1", "example.txt")
```

### Example Scripts

The following example scripts demonstrate different aspects of the file transfer system:

- `examples/remote_file_retrieval.py`: Demonstrates retrieving files from remote resources using `FileStack.get()`
- `examples/remote_file_retrieval_standalone.py`: Standalone version with simplified dependencies
- `examples/copy_decorator_example.py`: Shows how to use the `@copy` decorator for file transfers
- `examples/copy_file.py`: Basic example of copying files between resources

## Testing

The file transfer functionality includes comprehensive tests in `tests/test_remote_file_transfer.py` covering:

1. Retrieving files from local in-memory instances
2. Retrieving files from local on-disk instances
3. Retrieving files from remote resources
4. Handling multiple remote instances with fallback when the first resource fails
5. Error cases when no valid instances or routes are available

## Implementation Details

The file transfer system uses a job-based approach for transferring files between resources:

1. `submit_copy_job`: Submits a job to copy a file from one resource to another
2. `check_job_status`: Checks the status of a submitted job
3. Route finder: Determines the best path to transfer files between resources
4. `@copy` decorator: Orchestrates file transfers based on routes and job monitoring

The system is designed to be extensible and can be adapted for different transfer mechanisms and orchestration systems. 