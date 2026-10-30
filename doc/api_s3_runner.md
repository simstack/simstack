# Minimal workflow changes for the API-only Runner

Runner uses Server URL and a resource token. MongoDB is owned by Server. Remove Mongo clients, connection strings, `[parameters.db]` and database initialization from workflow repositories and resource environments. Do not copy DB settings into Docker, Apptainer or Slurm jobs.

Ordinary `context.db.find`, `find_one`, `save`, references and model hooks retain their interfaces and run through an HTTP-backed persistence adapter. The wire preserves BSON ObjectIds/datetimes and ODMantic write sets; raw atomic operations execute on Server. Avoid reaching into driver clients, sessions, administrative database methods or opening another Mongo connection. Replace those exceptional workflow calls with the existing facade operations, adding an explicit Server API only where genuinely necessary.

Keep workflow decorators, argument/result models, nested calls, parameters, resource assignments, container settings and error/logging conventions. `FileStack` call signatures remain; `in_memory` is accepted for compatibility but file bytes are never persisted in MongoDB. String/file constructors stage data locally, publication stores S3 metadata, and execution prefers a valid local copy before requesting a signed download URL.

Ensure the repository declares the intended Python version, locked dependencies, recursive Git submodules and optional dependencies in the existing per-resource configuration. Workflow environments receive the current pinned SimStack Core after resolving workflow dependencies. System binaries, licensed programs, HPC modules and resource startup commands remain machine configuration.

Test at least a native workflow, a nested/subworkflow call, a model save/load with references and a file-producing workflow; also test each Docker/Apptainer/Slurm path used by the repository. For versioning, queue a v1 task, upload v2, and verify the queued task and its children retain v1 while new submissions use v2. The UI does not add historical-version selection for new tasks.

Server loads repositories using the existing recursive model/node table builders in isolated temporary runtimes. It stores commit/submodule identity and catalogue metadata. Runner obtains the corresponding source on demand without GitLab credentials, then caches its pinned runtime. Removing a GitLab commit or revoking access can prevent restoring a missing cache; persistent Server source snapshots are intentionally absent.
