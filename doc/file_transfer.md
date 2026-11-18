# File transfers with FileStack

A `FileStack` identifies a file and tracks its available locations. By default, `from_local_file()` takes a local snapshot so the original file can change after staging. Publishing the value uploads its bytes to S3-compatible object storage. Server stores the file metadata and object reference in MongoDB.

## Stage and read a file

With an existing `result.csv` in the working directory, this example stages the file and makes a separate working copy. The path returned by `get()` can be passed to a calculation.

```python
from pathlib import Path
from simstack.models.files import FileStack

result = FileStack.from_local_file(Path("result.csv"))
local_path = result.get(local_dir=Path("inputs"))
```

## Read a published file

In an initialized SDK session, use `await context.server.values.publish_value(result)` to publish the staged file. A later session can retrieve its metadata with `await context.server.values.get_value(FileStack, result.id)`. Call `get()` on that value to obtain a local path. SimStack reuses a suitable local copy when available. Otherwise it downloads the published bytes through a short-lived, signed URL and checks their integrity.

For a previous run, open the file from the task's saved inputs or results to retrieve the version used by that run. The [workflow and migration guide](api_s3_runner.md) documents SDK initialization and migration of historical files. The [file model reference](source/files.rst) covers `FileStack`, `FileList` and `FileListModel`.
