# SimStack file transfers

File transfer is a bounded module inside SimStack Server, not a separately
deployed application. The Server owns metadata, authorization, claim checks,
temporary objects, and lifecycle transitions. Runners use HTTP only: no database
driver, database credentials, SSH routes, or direct-database fallback.

## File representation and storage

A `FileStack` contains compressed inline content or resource-local `FileInstance`
locations with opaque IDs, workdir-relative canonical paths, immutable size and
SHA256 checksum. `FileStack.from_local_file(path)` compresses incrementally and
switches to file-backed storage when its complete portable document exceeds the
Server-advertised capacity or an explicit request policy. External files are
copied into the resource workdir; the actual copied file path is persisted.
`make_copy=False` avoids a copy only if the source is already in that workdir.
Large runner `FileStack.from_string(...)` values are staged in the active task
directory instead of rejected by an arbitrary total-file-size limit.

MongoDB's physical BSON-document capacity still applies to inline documents.
The Server validates actual encoded BSON before writes. UI uploads switch to
file-backed storage if compressed content plus metadata does not fit. The whole
runner request is checked separately against an operator policy only if set;
factory-time checks cannot predict sibling results or the complete task envelope.

## Runner-to-runner lifecycle

1. The target execution worker calls `FileStack.get()`: inline content is
   decompressed locally and an available local location is validated and reused.
2. Otherwise it creates an idempotent transfer with a task/claim/attempt-bound
   execution token. The Server checks declared file inputs/dependencies.
3. The source `FileTransferService` discovers requests using its supervisor token
   and uploads with streaming HTTP. Large requests use `Content-Range`; proxy
   size rejections can reduce the chunk size.
4. Shared-filesystem locks serialize competing writers. HTTP 423 is retryable.
   The Server validates immutable size/checksum and rechecks the task claim
   before publication.
5. The target verifies a temporary download before atomic, non-overwriting
   publication, then completes the transfer. The target path must belong to the
   active task directory. The Server adds a reusable location with compare-and-set
   semantics and deletes staging objects.
6. A stale hydrated `FileStack` reads completed target metadata through the API
   and reuses the cache only after task-directory, size, and checksum checks.
   If that cache was deleted, an explicit restart API creates a separate,
   predecessor-derived idempotent successor. Previous completions cannot mutate
   the successor; changed local contents are rejected rather than overwritten.

API workers serving this module must share its staging filesystem and metadata.
A different storage backend needs an explicit adapter, not a guessed fallback.

## UI downloads and cleanup

UI downloads use inline data or a workdir-contained, resource-matching Server
location. Remote files use the same source upload service before streaming to
the browser. ZIP archives use it for remote members too and are assembled on
disk rather than entirely in RAM. Response cleanup completes staging requests.

Infrastructure failures leave source uploads recoverable. Invalid paths,
missing source files, or immutable-content mismatches fail explicitly. Stale
claims cannot upload, download, or publish locations. Expired requests are
rejected without waiting for a cleanup poll. Cleanup removes payload and partial
objects for expired transfers without sweeping neighbors.
Normal runner shutdown cancels queued uploads, signals active streaming uploads
cooperatively, and awaits their I/O threads before returning. Interrupted
transfers retain their claim and immutable metadata for the next source poll.

## Server artifact files

Registered server artifact callables receive a request-scoped file I/O port,
not a global runner or repository context. Remote files in the argument graph
are prepared asynchronously through the bounded transfer service before the
callable runs; both sync and async functions can then use `FileStack.get()`.
Inline/local files and file-backed factories use explicit Server resource and
storage configuration. Each rendering gets an isolated artifact-files directory;
file-backed output locations remain valid after the callback returns. Runners
cannot install this server-side port or bypass their claim-bound API path.

## Configuration and verification

Optional `SIMSTACK_FILE_TRANSFER_MAX_BYTES` and `SIMSTACK_RUNNER_MAX_PAYLOAD_BYTES`
policies default to unset: no arbitrary application-level total-file/request
cap, while physical inline-storage constraints remain.

Transfer settings include `SIMSTACK_FILE_TRANSFER_TMP_DIR` (default
`<server-workdir>/file-transfers`), `SIMSTACK_FILE_TRANSFER_TTL_SECONDS`,
`SIMSTACK_FILE_TRANSFER_UPLOAD_CHUNK_BYTES`,
`SIMSTACK_FILE_TRANSFER_REQUEST_TIMEOUT_SECONDS`,
`SIMSTACK_FILE_TRANSFER_TIMEOUT_SECONDS`, and
`SIMSTACK_FILE_TRANSFER_POLL_SECONDS`. UI waits use
`SIMSTACK_UI_FILE_TRANSFER_TIMEOUT_SECONDS` and
`SIMSTACK_UI_FILE_TRANSFER_POLL_SECONDS`. Malformed/non-positive intervals fail
explicitly rather than silently reverting to defaults.

Core tests cover containment, copy semantics, complete inline-envelope sizing,
atomic verified downloads and retries. Server tests cover claims/resources,
concurrent uploads, chunks, completion replay, neighboring-task rejection,
expiration and partial cleanup. Real loopback HTTP tests include runner transfer,
UI download, disk-backed archives and separate driver-free source/target client
subprocesses, including partial-upload shutdown/resume and repeated deleted-cache
restarts. Set `SIMSTACK_RUNNER_TEST_PYTHON` to a driver-free Core interpreter
for the subprocess test.
