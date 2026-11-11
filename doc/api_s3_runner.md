# Minimal workflow changes for the API-only Runner

Runner uses Server URL and a resource token. MongoDB is owned by Server. Remove Mongo clients, connection strings, `[parameters.db]` and database initialization from workflow repositories and resource environments. Do not copy DB settings into Docker, Apptainer or Slurm jobs.

Workflow code uses explicit Server operations through `context.server`: scientific values in `.values`, task lifecycle in `.tasks`, Slurm submissions in `.slurm`, and Runner resource control in `.resource`. `.requests` holds HTTP credentials and request identity. There is no `context.db`, generic repository, compatibility alias, model `save` method or persistence hook in Runner. Collection names, database queries, sessions and atomic storage mutations remain in Server.

Import domain classes from `simstack.models` (`Model`, `EmbeddedModel`, `Field`, `Reference`, `ObjectId`) instead of `odmantic` or `bson`. These are pure Pydantic values. Identity remains a string; references, concrete subtypes, units and scientific scalar values survive explicit publication and resolution. Iteration, properties, serialization and object construction perform no network access. Replace class-field query expressions with the named scientific operations, such as `query_records(DataSet, "by_field_name", field_name="results")`.

Server initializes the installed Core model/node catalogue for each workspace and publishes the pinned repository's catalogue using local recursive builders and an isolated collector. Initialization retains existing workflow overrides, defaults and favorites. Runner imports approved workflow classes from that catalogue and performs scientific transformations locally. It sends complete typed value graphs through `.values.publish_value` or `.values.publish_values`; Server validates and persists them. Read values with `.values.get_value` or `.values.resolve_values`. Update workflow dependency declarations and lockfiles together with Core so removed ODMantic and repository imports cannot return transitively.

For a user SDK session, use a user access token. Resource Runner tokens are obtained through enrollment and are used only by Runner processes:

```python
import os
from simstack.core.context import context
from simstack.server_api import ServerAPI
from simstack.util.importer import import_class

server = ServerAPI.for_user(
    os.environ["SIMSTACK_SERVER_URL"],
    os.environ["SIMSTACK_USER_TOKEN"],
    resource="cluster-a",
)
await context.initialize(server=server, resource="cluster-a")
Energy = await import_class("workflow.models.Energy", context.server.values)
result = await context.server.values.publish_value(Energy(value=-76.4, unit="Hartree"))
loaded = await context.server.values.get_value(Energy, result.id)
```

`workflow.models.Energy` must belong to the approved repository catalogue. Nested workflow calls, task caching, argument/result models, parameters, resource assignments, container settings and error/logging conventions remain supported. Task changes use the task API with their execution identity; a scientific publication cannot rewrite task status or stored errors.

## Preserve workflow behavior when replacing database calls

Keep the numerical algorithm, node graph, concurrency, result models/units, checkpoint selection and resource parameters. Replace persistence calls and imports; do not rename a requested resource just to make it pass validation. Server applies assignment rules before validating the effective target and inheriting its allocation. For example, `Parameters(resource="local", queue="default", in_docker=True)` remains valid when a profile rule maps that request to a configured resource. A child targeting `self` inherits the persisted parent's allocation; an arbitrary `parent_parameters` argument cannot override that Server decision.

Replace `context.db.find_one(Model, Model.id == identity)` with `await context.server.values.get_value(Model, identity)`, and an explicit scientific save with `await context.server.values.publish_value(value)`. Typed queries accept declared nested model fields:

```python
datasets = await context.server.values.query_records(
    DataSet,
    filters=[{"field": "metadata.field_name", "operator": "eq", "value": "simple-set"}],
    order_by=[{"field": "metadata.field_name", "direction": "asc"}],
    skip=0,
    limit=20,
)
```

Filters are combined with AND. Supported comparisons are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in` and `not_in`; the last two take lists. Nested paths follow declared embedded fields, not arbitrary MongoDB expressions or joins through scientific references. A reference's declared primary-field path compares its stored identity. Pagination and ordering run on Server. If a required workflow query is unsupported, extend the typed Server contract; do not fetch every scientific record and filter it in Runner or change the scientific model to fit the API.

`NodeRegistry` is task state, not a scientific record. Replace task searches with `context.server.tasks.query_tasks(status=..., parent_id=..., name=..., call_path=..., skip=..., limit=...)`. `call_path` uses the existing resource-rule pattern semantics: `.calculator` matches a suffix, while `.workflow.calculator` matches that complete path. There is no arbitrary task save operation.

Checkpoint recovery keeps its scientific decisions in the workflow. Query timeout tasks, read their scientific snapshots through `.values`, and choose/rebuild the restart input as before. Launch the calculator under the **live coordinator**, adding `recovery_of=original` instead of pretending the timed-out task is still executing:

```python
tasks = context.server.tasks
coordinator = await tasks.get_task(node_runner.task_id)
await calculator(
    recovery_input,
    **{**kwargs, "parent_id": coordinator.id, "call_path": original.call_path,
       "parameters": child_parameters, "recovery_of": original},
)
children = await tasks.query_tasks(
    parent_id=coordinator.id, name="calculator", status="completed",
)
source = max(
    (child for child in children if original.id in child.parent_ids),
    key=lambda child: child.created_at,
)
recovered = await tasks.adopt_recovery_result(original, source, coordinator=coordinator)
```

Here `calculator`, its catalogue name, `recovery_input` and `child_parameters` are the workflow's existing calculator and restart settings. Server pins the recovery child to the original task's repository revision, creates a fresh calculation, and retains both original/coordinator lineage. If the coordinator uses a different revision, recovery is scheduled through the original revision's prepared executor rather than running inline in the coordinator's interpreter. Adoption accepts only that completed recovery child's immutable result and preserves the original task ID, inputs, custom name and timeout attempt history. A competing rerun, start or cancellation rejects the operation; failed recovery leaves the original timeout unchanged. Fresh task reads expose the adopted computation result, while an already-held exact timeout-attempt DTO still addresses its historical evidence. A later Rerun retains both histories.

For adaptation acceptance, compare scientific values, units, DAG/parallelism, effective placement, result names, file bytes, cache reuse and recovery against the original workflow. A successful Jia run alone is insufficient; include another model/query workflow and a real checkpoint-recovery workflow using the execution backends that repository supports.

A standalone SDK call targeting `self` or the SDK's own resource can execute locally when Server resolves it to the default execution route. Server grants that attempt a separate execution credential; the SDK launches its child process without a resource Runner or database connection. The grant is restricted to that execution tree and is revoked when the attempt is superseded or its user is disabled. It remains valid through long calculations after the SDK's user access token expires. Remote resources, Slurm and cloud routes continue through their scheduled executors. A failed cached task retains its stored error; `force_rerun=True` explicitly requests another calculation.

`FileStack` constructors stage bytes locally. Explicit scientific publication uploads them to S3 through the object API before publishing their metadata, preserving approved subclasses and their scientific fields. User uploads use user authorization, admitted local executions use their execution credential, and Runner uploads use the resource credential. SDK paths are not registered as managed resource copies. File reads prefer an available local copy and otherwise download through a signed object URL. Cleanup removes only expired cached copies; authoritative local files remain available. `content` and `in_memory=True` are rejected when constructing a stored payload; file bytes are never embedded in MongoDB or a scientific value graph.

Copying a published `FileStack` to a new identity secures an independent object immediately through the current caller's object API. The copy therefore survives archival of its source before scientific publication. This operation performs synchronous I/O; asynchronous workflows can use `await asyncio.to_thread(model_copy, value)` to keep their event loop responsive. A failed object copy raises without changing the source.

Historical inline files require an explicit Server migration before the new readers are enabled. Back up and drain the workspace, then run `python -m server.migrations.inline_files --email <workspace-owner>` from the Server environment. This default dry run inventories canonical files and registered embedded snapshots and verifies their compressed bytes. Run the same command with `--apply` to upload and verify object bytes before replacing inline content with S3 metadata. IDs, scientific hashes and surrounding history are retained. Embedded-only files receive canonical metadata so the normal download-by-ID API can retrieve them. The complete workspace is preflighted before any upload or metadata write: different byte versions sharing one FileStack ID are rejected and require an explicit version migration policy; this migration does not silently choose one historical version. Repeating the command skips migrated records. Missing bytes, unknown file schemas, corrupt uploads or concurrent edits stop the migration while retaining the original inline bytes for the affected document. Complete a dry run with zero remaining files and verify representative historical workflows before enabling the new readers; there is no runtime fallback to old inline storage.

Ensure the repository declares the intended Python version, locked dependencies, recursive Git submodules and optional dependencies in the existing per-resource configuration. Workflow environments use the Core version installed with Runner after resolving workflow dependencies; an older workflow dependency does not replace that platform package. System binaries, licensed programs, HPC modules and resource startup commands remain machine configuration.

Every runnable pinned repository declares its SDK contract in `pyproject.toml`:

```toml
[tool.simstack]
runner-protocol = 3
```

Server checks this before dependency preparation and workflow imports, and records the verified protocol in the published revision. Runner checks the same manifest before preparing or advertising a runtime. A plain Python script repository can contain just this table without a `[project]` table; it keeps the existing environment based on installed platform dependencies. Missing or incompatible declarations reject new work explicitly; stored results remain readable.

Test at least a native workflow, a nested/subworkflow call, scientific publication/resolution with references and a file-producing workflow; also test each Docker/Apptainer/Slurm path used by the repository. For versioning, queue a v1 task, publish v2, and verify the queued task and its children retain v1 while new submissions use v2. The UI does not add historical-version selection for new tasks.

Connect a repository in the UI with its SSH or HTTPS clone URL and selected branch. Private SSH access uses one Ed25519 key per SimStack user: add the public key to the Git service, inspect and explicitly confirm the actual host's fingerprints, then enroll each Runner using its short-lived, one-use installer token over HTTPS. Enrollment installs the same user's private key locally with permissions `0600`; an ordinary Runner API token cannot download it again. Unknown or changed host keys are rejected. Submodules and private Git dependencies need access and trusted host keys for their own hosts as well.

Server loads pinned Git revisions using the existing recursive model/node table builders in isolated runtimes, with SSH credentials available to Git and `uv sync --locked` but excluded from discovery. Publication retains a prepared Server source/dependency runtime for dynamic UI schema evaluation. Its identity includes the owner, source, extras and installed platform environment. Schema GET reads published JSON Schema and evaluates dynamic `ui_schema()` in a fresh isolated worker using that prepared runtime; it does not clone Git or install dependencies. Missing or incompatible prepared runtimes require an explicit repository refresh, including revisions published before this preparation contract. Dynamic schemas are not permanently cached by revision. Other validation, table and artifact operations continue to restore their pinned runtime through their existing path.

Before enabling this schema read path on an existing installation, prepare every ready revision still needed by historical task views: `POST /workflow-repositories/{revision}/prepare` with JSON body `{"target":"server"}`. This prepares the stored commit, submodules, extras and inspection resource and updates only its Server runtime pointer after success. It does not activate that historical version or change Runner preparation targets. The same operation repairs a missing runtime after replacing Server's environment/work directory. The existing default `target="runner"` operation continues preparing execution resources.

Runner receives version preparation commands through the API control poll and obtains the pinned source directly from Git. It verifies the commit, recursive submodules and tags, runs `uv sync --locked` with its resource extras, and retains a separate source/runtime cache for each revision, extras profile and Core build. Readiness verifies the Core actually loaded by the prepared interpreter. Server admits tasks for that resource only after successful preparation is acknowledged as ready. Preparation waits while persisted tasks show that the resource is busy; existing tasks and children continue using their original versions. Startup inventories verified runtimes without modifying them; missing or broken environments are prepared or repaired through the same control path. Standalone SDK execution verifies its pinned local checkout and uses the SDK environment.

Prepared environments retain a copy of their Core build rather than a link to the mutable Runner installation. Updating Runner does not change the code loaded later by an existing prepared environment.

Before switching an existing installation to the fenced execution protocol, pause new submissions and drain active executions using the installed Server and Runner versions. For every workspace, confirm there are no tasks in `retrieved`, `running`, `slurm_queued`, `slurm_running` or `terminating`; also check each resource's Slurm queue and accounting so an exited launcher cannot hide a live allocation. Preserve the original job IDs, results and terminal outcomes. Upgrade only after both checks agree that those executions have stopped. Waiting `submitted` tasks can then be admitted by the new Runner. Do not synthesize submission, claim or start identities for already running legacy jobs: an ID backfill cannot prove who launched the allocation. This is a drain policy, not automatic migration of active jobs.

Slurm's configured `export` option is preserved, including `NONE`, `NIL` and explicit variable lists. The batch wrapper sources only its required SimStack launcher environment from a private `0600` file in the shared job work directory; keep that directory available until the allocation has terminated. The source step follows user startup commands so the launcher retains its admitted execution identity. Scheduler observations use Server receipt times for completion grace; Runner timestamps remain diagnostic evidence.

Each Slurm batch incarnation obtains a separate execution identity from Server before it runs science. A higher `SLURM_RESTART_COUNT`, including a restart enabled by the cluster default, retires the interrupted attempt and its unfinished inline children; completed child results remain reusable. Duplicate batch processes and previous incarnations cannot change the admitted attempt. Terminal accounting must describe the current or a later restart; a later batch that failed before API admission can therefore close the interrupted task. Older accounting cannot close a newer admitted execution. A completed scientific task is not recomputed merely because its batch script is requeued.

Slurm job arrays are not a task parameter: a single SimStack task has one input and result contract. Use explicit child tasks or `MassRunner` for parallel scientific calculations with separate task identities and results. The removed `array` field, array options in `extra_options`, and `SBATCH_ARRAY_INX` are rejected rather than launching several writers for one task.

Managed native launches persist a separate observation lease before spawning. The worker retains that lease after start acknowledgement, so a restarted Runner can distinguish the original process lifetime from a reused PID. Recovery records a missing-worker outcome only after scientific processes are verified stopped: the original POSIX group is empty, or the exact Windows Job has no active processes. Reattached observers report unavailable exit codes and signals as unknown. A surviving or unverifiable process group retains its evidence and cannot produce a false cancellation acknowledgement. An unstarted abandoned claim remains eligible for normal launch recovery.

Windows native execution requires Windows 10 or Windows Server 2016 or later for atomic Job membership at process creation. Science workers and their descendants belong to an execution-specific Job; shared child executions receive independent Jobs. The caller and controller retain handles for verified cleanup after either exits. If all owners disappear and the original named Job cannot be reopened, recovery cannot prove that termination has finished: it retains the journal and cancellation fence. Inspect the resource and verify the original execution has stopped before manually reconciling that attempt; a missing or reused PID alone is insufficient.

Core CI tests both source checkouts against the reviewed Server commit in the `SIMSTACK_SERVER_TEST_REF` repository variable. This must be a full commit SHA, not a moving branch. Its HTTP integration and real Runner gates run on Linux and Windows.
