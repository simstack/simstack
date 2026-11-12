"""Durable claim receipts and execution identity for API workers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

_execution = ContextVar("simstack_execution", default=None)
logger = logging.getLogger("NodeRunner")


def _actor(api):
    requests = api.requests
    resource = requests.resource
    bootstrap = requests.bootstrap_data
    account_id = bootstrap.get("account_id") if bootstrap else None
    if not account_id or not resource:
        raise ValueError("A launch receipt requires a bootstrapped account and resource")
    actor = {
        "server_url": requests.server_url,
        "account_id": str(account_id),
        "resource": str(resource),
        "authorization": requests.authorization,
    }
    if requests.authorization == "local":
        actor["credential_fingerprint"] = hashlib.sha256(requests.token.encode()).hexdigest()
    return actor


def _directory(server_url):
    workdir = os.environ.get("SIMSTACK_RUNNER_WORKDIR")
    credentials = os.environ.get("SIMSTACK_RUNNER_CREDENTIALS")
    root = (
        Path(workdir)
        if workdir
        else Path(credentials).parent
        if credentials
        else Path.home() / ".local/state/simstack"
    )
    key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
    directory = root / ".claims" / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _receipt_path(api, task_id):
    if len(str(task_id)) != 24 or any(
        c not in "0123456789abcdef" for c in str(task_id)
    ):
        raise ValueError("Invalid claim task identity")
    account = hashlib.sha256(_actor(api)["account_id"].encode()).hexdigest()[:16]
    return _directory(api.requests.server_url) / (account + "-" + str(task_id) + ".json")


def reserve_launch(api, task_id):
    """Reserve the launch until the last owner, including its child, closes it."""
    path = _receipt_path(api, task_id).with_suffix(".launch")
    return _reserve_path(path)


def _reserve_path(path):
    """Acquire an inheritable process-lifetime lease at an existing scope."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        # An exclusive file handle survives its launcher's death when inherited.
        # Windows byte-range locks, unlike Unix flock, do not have that property.
        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        generic_read_write, open_always, normal_file = 0xC0000000, 4, 0x80
        handle = create(str(path), generic_read_write, 0, None, open_always, normal_file, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in {32, 33}:  # Sharing or lock violation: another launch owns it.
                return None
            raise ctypes.WinError(error)
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), "a+b")

    import fcntl

    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except Exception:
        handle.close()
        raise
    # Do not explicitly LOCK_UN: that would also unlock the child's inherited fd.
    return handle


@contextmanager
def inherit_launch_reservation(reservation, environment):
    """Transfer the live launch reservation atomically with subprocess creation."""
    inherited = os.environ.get("SIMSTACK_LAUNCH_RESERVATION")
    if reservation is None:
        # A run_node process can hand the same launch to Docker/Apptainer before
        # its own start. Never pass a parent's handle to a different execution.
        identity = ("TASK", "GENERATION", "CLAIM", "START")
        if not inherited or not all(os.environ.get("SIMSTACK_EXECUTION_" + field)
                                    for field in ("TASK", "CLAIM", "START")) or any(
            environment.get("SIMSTACK_EXECUTION_" + field) != os.environ.get("SIMSTACK_EXECUTION_" + field)
            for field in identity
        ):
            environment.pop("SIMSTACK_LAUNCH_RESERVATION", None)
            yield {}
            return
    if os.name != "nt":
        descriptor = reservation.fileno() if reservation is not None else int(inherited)
        environment["SIMSTACK_LAUNCH_RESERVATION"] = str(descriptor)
        yield {"pass_fds": (descriptor,)}
        return

    import msvcrt
    import subprocess

    handle = msvcrt.get_osfhandle(reservation.fileno()) if reservation is not None else int(inherited)
    environment["SIMSTACK_LAUNCH_RESERVATION"] = str(handle)
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": [handle]}
    os.set_handle_inheritable(handle, True)
    try:
        yield {"startupinfo": startup}
    finally:
        os.set_handle_inheritable(handle, False)


def receipt(
    api,
    task_id,
    *,
    begin=False,
    revision=None,
    generation=None,
    inline=False,
    publication=None,
    request_id=None,
    claim_id=None,
    recovered_local=False,
):
    actor = _actor(api) if begin else None
    path = _receipt_path(api, task_id)
    from simstack.runner_runtime.runner_credentials import credential_lock

    with (
        path.with_suffix(".lock").open("a+") as lock,
        credential_lock(lock, exclusive=True),
    ):
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            value = None
        if (
            begin
            and value is not None
            and value.get("generation") == generation
            and (claim_id is None or value.get("claim_id") == claim_id)
            and (value.get("journal_version") != 1 or value.get("actor") != actor)
            and not recovered_local
        ):
            raise ValueError("Existing launch receipt does not match its caller")
        if begin and (
            value is None
            or value.get("generation") != generation
            or (claim_id is not None and value.get("claim_id") != claim_id)
            or (recovered_local and value.get("actor") != actor)
        ):
            value = {
                "journal_version": 1,
                "actor": actor,
                "task_id": str(task_id),
                "claim_id": claim_id or str(uuid4()),
                "revision": revision,
                "generation": generation,
                "state": "pending",
            }
            if recovered_local:
                value["recovered_local"] = True
            if inline:
                value["inline"] = True
            if publication is not None:
                value["publication"] = publication
                value["request_id"] = request_id or str(uuid4())
            _write(path, value)
    return value


def finish(api, value, *, claimed):
    path = _receipt_path(api, value["task_id"])
    from simstack.runner_runtime.runner_credentials import credential_lock

    with (
        path.with_suffix(".lock").open("a+") as lock,
        credential_lock(lock, exclusive=True),
    ):
        try:
            current = json.loads(path.read_text())
        except FileNotFoundError:
            current = None
        if current and current["claim_id"] == value["claim_id"]:
            if claimed:
                _write(path, {**value, "state": "claimed"})
            else:
                path.unlink(missing_ok=True)


def pending(api):
    for path in _directory(api.requests.server_url).glob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("Unreadable launch journal %s retained", path.name)
            continue
        if not isinstance(value, dict):
            logger.warning("Unknown launch journal %s retained", path.name)
            continue
        if value.get("state") in {"pending", "claimed"}:
            yield value


def record_spawn(api, entry, process_id):
    """Remember the local child before any network acknowledgement."""
    from simstack.runner_runtime.runner_credentials import credential_lock
    path = _receipt_path(api, entry.id)
    with path.with_suffix(".lock").open("a+") as lock, credential_lock(lock, exclusive=True):
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return
        _write(path, {**value, "process_id": process_id,
            "start_id": getattr(entry, "_execution_start_id", None)})


def record_container_launch(api, entry, name, *, daemon_id=None):
    """Persist external identity before Docker can accept an indeterminate run."""
    from simstack.runner_runtime.runner_credentials import credential_lock
    path = _receipt_path(api, entry.id)
    execution = api.tasks.execution(entry)
    with path.with_suffix(".lock").open("a+") as lock, credential_lock(lock, exclusive=True):
        value = json.loads(path.read_text()) if path.exists() else None
        if value is None or any(value.get(field) != execution.get(field)
                                for field in ("generation", "claim_id")):
            raise ValueError("Container launch requires its original claim receipt")
        if value.get("container_name") and (value["container_name"] != name or value.get("start_id") != execution["start_id"]):
            raise ValueError("Container launch differs from its recorded execution")
        recorded = bool(value.get("container_name"))
        if value.get("container_daemon") not in {None, daemon_id}:
            raise ValueError("Container launch belongs to a different Docker daemon")
        _write(path, {**value, "container_name": name, "start_id": execution["start_id"],
            **({"managed_container": True, "container_daemon": daemon_id} if daemon_id else {})})
        return recorded


async def acquire_work(server, capacity=1):
    """Persist one acquisition identity until its reply is safely recorded."""
    actor = _actor(server)
    directory = _directory(server.requests.server_url)
    if (directory / "work-acquisition.json").exists():
        logger.warning("Unscoped work acquisition journal retained")
    scope = hashlib.sha256(json.dumps(actor, sort_keys=True).encode()).hexdigest()[:16]
    path = directory / ("work-acquisition-" + scope + ".json")
    # One process owns the entire intent/reply/receipt transaction. Holding a
    # nonblocking lease across HTTP also avoids blocking another event loop.
    reservation = _reserve_path(path.with_suffix(".acquire"))
    if reservation is None:
        return []
    try:
        intent = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "journal_version": 1,
                "actor": actor,
                "request_id": str(uuid4()),
                "capacity": capacity,
            }
        )
        if intent.get("journal_version") != 1 or intent.get("actor") != actor:
            raise ValueError("Work acquisition journal does not match its caller")
        _write(path, intent)
        tasks = await server.tasks.acquire_work(
            intent["capacity"], request_id=intent["request_id"]
        )
        for entry in tasks:
            value = receipt(
                server,
                entry.id,
                begin=True,
                revision=entry.repository_revision,
                generation=getattr(entry, "_execution_generation", None),
                claim_id=getattr(entry, "_execution_claim_id", None),
            )
            finish(server, value, claimed=True)
        path.unlink(missing_ok=True)
        return tasks
    finally:
        reservation.close()


def acknowledge_start(server, entry, started):
    """Release a started launch while retaining its local Docker observation."""
    if started is None:
        return
    value = receipt(server, entry.id)
    if (
        value is not None
        and value["claim_id"] == server.tasks.execution(entry)["claim_id"]
    ):
        # Docker can outlive both SDK and host controller. Keep its exact
        # local observation after start, but release the launch lease below.
        if not (value.get("container_name") and (value.get("managed_container")
                or value.get("actor", {}).get("authorization") in {"user", "local"})):
            finish(server, value, claimed=False)
    inherited = os.environ.get("SIMSTACK_LAUNCH_RESERVATION")
    execution = server.tasks.execution(entry)
    if inherited is not None and all(
        os.environ.get("SIMSTACK_EXECUTION_" + key.upper()) == execution.get(field)
        for key, field in (("task", "task_id"), ("claim", "claim_id"), ("start", "start_id"))
    ):
        descriptor = int(os.environ.pop("SIMSTACK_LAUNCH_RESERVATION"))
        if os.name == "nt":
            import msvcrt
            descriptor = msvcrt.open_osfhandle(descriptor, os.O_RDWR | os.O_BINARY)
        os.close(descriptor)


async def reconcile_claim(server, value):
    if value.get("managed_container") and value.get("container_name"):
        return await _recover_managed_container(server, value)
    if (server.requests.authorization in {"user", "local"}
            and value.get("process_id") and value.get("start_id")):
        # The lease serializes recovery and excludes a live prestart launcher.
        # Started Docker observations retain no additional launch reservation.
        from simstack.server_api.http_transport import RunnerAPIError
        try:
            current = await server.tasks.get_task(value["task_id"])
        except RunnerAPIError as exc:
            if exc.status_code == 404 and value.get("container_name"):
                # A normal stop ACK may permit DELETE before this observer
                # drains. Retire only this deleted task's matching receipt.
                return {"state": "stale"}
            raise
        owner = getattr(current, "_execution_owner", None)
        execution = {key: value.get(key) for key in ("task_id", "generation", "claim_id", "start_id")}
        if owner == {"execution": execution, "process_id": value["process_id"]}:
            if value.get("container_name"):
                # An admitted owner's planned start is not a scientific start
                # ACK. Its Docker execution can outlive the launcher lease.
                import asyncio
                import subprocess
                from simstack.core.run_docker import docker_execution_name, inspect_docker_execution
                if docker_execution_name(server, execution) != value["container_name"]:
                    raise ValueError("Container journal does not match its original execution")
                try:
                    observed = await asyncio.to_thread(inspect_docker_execution, value["container_name"])
                    if observed is not None and observed["State"]["Status"] == "created":
                        # A late daemon start races this non-force removal. If
                        # it wins, retain the owner and inspect again later.
                        removed = await asyncio.to_thread(subprocess.run,
                            ["docker", "rm", observed["Id"]], capture_output=True, timeout=10)
                        if removed.returncode:
                            return {"state": "pending"}
                        observed = None
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    logger.warning("Local Docker launch %s awaits execution evidence", value["task_id"], exc_info=True)
                    return {"state": "pending"}
                if observed is not None:
                    if observed["State"]["Status"] not in {"exited", "dead"}:
                        return {"state": "pending"}
                    return await server.tasks.recover_local_owner(value,
                        container_stopped=True, exit_code=observed["State"]["ExitCode"])
                if (getattr(current, "_execution_start_id", None) is not None
                        or current.status in {"completed", "failed", "timeout", "recovered"}):
                    return await server.tasks.recover_local_owner(value, container_stopped=True)
            recovered = await server.tasks.recover_local_owner(value)
            if value.get("container_name") and recovered.get("state") == "active":
                # An original start committed after the absence observation.
                # Its owner must still be observed, never launched again.
                return {"state": "pending"}
            if recovered.get("state") != "unowned":
                return {**recovered, "recovered_local": True}
        elif (current is not None and getattr(current, "_execution_claim_id", None) != value["claim_id"]
                and getattr(current, "_execution_recoverable_local", False)):
            # The owner recovery committed before its reply/receipt survived.
            recovered = await server.tasks.recover_local_launch(value)
            return {**recovered, "recovered_local": True}
    if value.get("recovered_local"):
        # A recovered launcher can itself die after physical-owner admission.
        # Reconcile that owner above before treating this as an unowned handoff.
        return await server.tasks.recover_local_launch(value)
    def prepared_submission(entry):
        if entry is None or entry.parameters.queue != "slurm-queue":
            return False
        from simstack.runner_runtime.slurm_submission_intents import read_intent
        intent = read_intent(server, entry.id)
        if intent is None or intent["phase"] != "prepared":
            return False
        current = server.tasks.execution(entry)
        planned = intent["execution"]
        if (any(current.get(key) != planned.get(key) for key in ("task_id", "generation", "claim_id"))
                or current.get("start_id") not in {None, planned.get("start_id")}):
            return False
        # A lost reservation reply may leave Server without its planned start
        # yet. Resume that same intent; allocating another ID would race it.
        object.__setattr__(entry, "_execution_start_id", planned["start_id"])
        return True

    async def container_launch(state):
        entry = state.get("task")
        if not value.get("container_name") or entry is None or state.get("state") not in {"claimed", "recoverable"}:
            return state
        execution = server.tasks.execution(entry)
        if getattr(entry, "_execution_start_id", None) is not None:
            return state
        execution["start_id"] = value["start_id"]
        from simstack.core.run_docker import docker_execution_name, inspect_docker_execution
        if docker_execution_name(server, execution) != value["container_name"]:
            raise ValueError("Container journal does not match its original execution")
        try:
            import asyncio
            import subprocess
            observation = await asyncio.to_thread(inspect_docker_execution, value["container_name"])
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            logger.warning("Docker launch %s awaits execution evidence", value["task_id"], exc_info=True)
            return {"state": "pending"}
        if observation is not None and observation["State"]["Status"] not in {"created", "exited", "dead"}:
            return {"state": "pending"}
        object.__setattr__(entry, "_execution_start_id", value["start_id"])
        if observation is not None and observation["State"]["Status"] in {"exited", "dead"}:
            status = observation["State"]["ExitCode"]
            from simstack.server_api.http_transport import RunnerAPIError
            try:
                await server.tasks.fail_attempt(entry, f"Container exited before recording its outcome (code {status})",
                    phase="container", exit_code=status)
            except RunnerAPIError as exc:
                if exc.status_code != 409:
                    raise
            return {"state": "terminal"}
        return state

    if value.get("publication"):
        known = await server.tasks.get_request(value["request_id"])
        if known["state"] == "rejected":
            return {"state": "rejected"}
        if known["state"] == "applied":
            result = known["result"]
        else:
            publish = (server.tasks.create_child if value["publication"].get("parent_execution")
                       else server.tasks.submit_root)
            result = await publish(
                value["publication"], request_id=value["request_id"]
            )
        entry = result.get("task")
        if result.get("disposition") != "launch_inline":
            return {
                "state": "terminal"
                if result.get("disposition") == "reused_complete"
                else "stale"
            }
        # A replayed publication is historical; only its current claim can launch.
        current = await server.tasks.get_task(entry.id)
        if current is None:
            return {"state": "publication_pending"}
        if getattr(current, "_execution_claim_id", None) != getattr(
            entry, "_execution_claim_id", None
        ) or getattr(current, "_execution_generation", None) != getattr(
            entry, "_execution_generation", None
        ):
            return {"state": "stale"}
        prepared = prepared_submission(current)
        if (getattr(current, "_execution_start_id", None)
                and getattr(current, "_execution_submission_state", None) != "prepared"
                and not prepared):
            return {"state": "active"}
        if current.status not in {"retrieved", "submitted"}:
            return {"state": "terminal"}
        if result.get("local_execution_token"):
            object.__setattr__(current, "_execution_token", result["local_execution_token"])
        return await container_launch({"state": "claimed", "task": current})
    result = await server.tasks.reconcile_attempts(claims=[value], outcomes=[])
    states = result.get("claims", [])
    state = states[0] if states else {"state": "pending"}
    prepared = prepared_submission(state.get("task"))
    if state.get("state") == "active" and prepared:
        return {**state, "state": "claimed"}
    return await container_launch(state)


async def _recover_managed_container(server, value):
    """Observe the exact daemon owner after START, including Runner restart."""
    import asyncio
    import subprocess
    from simstack.core.run_docker import docker_daemon_identity, docker_execution_name, inspect_docker_execution
    from simstack.server_api.http_transport import RunnerAPIError
    execution = {key: value.get(key) for key in ("task_id", "generation", "claim_id", "start_id")}
    if docker_execution_name(server, execution) != value["container_name"]:
        raise ValueError("Container journal does not match its original execution")
    try:
        if not value.get("container_daemon") or await asyncio.to_thread(docker_daemon_identity) != value["container_daemon"]:
            return {"state": "pending"}
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return {"state": "pending"}
    try:
        entry = await server.tasks.get_task(value["task_id"])
    except RunnerAPIError as exc:
        if exc.status_code == 404:
            return {"state": "stale"}  # DELETE already required the stop ACK.
        raise
    current = server.tasks.execution(entry)
    started_id = getattr(entry, "_execution_start_id", None)
    if (any(current.get(key) != execution[key] for key in ("task_id", "generation", "claim_id"))
            or current.get("start_id") not in {None, execution["start_id"]}):
        return {"state": "stale"}
    try:
        observed = await asyncio.to_thread(inspect_docker_execution, value["container_name"])
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return {"state": "pending"}
    if observed is not None and observed["State"]["Status"] not in {"created", "exited", "dead"}:
        return {"state": "pending"}
    object.__setattr__(entry, "_execution_start_id", execution["start_id"])
    if (started_id is None and entry.status in {"retrieved", "submitted"}
            and (observed is None or observed["State"]["Status"] == "created")):
        # No scientific start occurred. Retry the same idempotent intent/name.
        return {"state": "claimed", "task": entry}
    if entry.status not in {"completed", "failed", "timeout", "recovered", "terminating"}:
        code = observed["State"]["ExitCode"] if observed is not None else None
        try:
            entry = await server.tasks.fail_attempt(entry,
                "Container stopped without recording its outcome", phase="container", exit_code=code)
        except RunnerAPIError as exc:
            if exc.status_code == 409:
                return {"state": "pending"}
            raise
        # Fence a potentially late START before treating daemon absence as stop.
        # If START won that race, its container must now be visible to inspection.
        try:
            observed = await asyncio.to_thread(inspect_docker_execution, value["container_name"])
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            return {"state": "pending"}
    if observed is not None:
        if observed["State"]["Status"] == "created":
            try:
                removed = await asyncio.to_thread(subprocess.run, ["docker", "rm", observed["Id"]],
                    capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return {"state": "pending"}
            if removed.returncode:
                return {"state": "pending"}
        elif observed["State"]["Status"] not in {"exited", "dead"}:
            return {"state": "pending"}
    object.__setattr__(entry, "_execution_start_id", execution["start_id"])
    try:
        await server.tasks.termination_confirmed(entry, entry.error or "Scientific execution stopped")
    except RunnerAPIError:
        return {"state": "pending"}
    return {"state": "terminal"}


async def recover_launches(api, *, task_id=None, observed=None):
    """Recover abandoned publications without releasing the executor's lock."""
    recovered = []
    actor = _actor(api)
    if actor["authorization"] in {"user", "local"}:
        from simstack.runner_runtime import native_observations
        await native_observations.recover(api, task_id=task_id)
    for value in list(pending(api)):
        if task_id is not None and value.get("task_id") != str(task_id):
            continue
        if value.get("journal_version") != 1 or not isinstance(value.get("actor"), dict):
            logger.warning("Unscoped or unknown launch journal for %s retained", value.get("task_id"))
            continue
        foreign = value["actor"] != actor
        # Only Server can transfer a shared local publication after its original
        # physical parent stopped. Never replay another actor's command journal.
        transferable = (foreign and actor["authorization"] in {"user", "local"}
            and value["actor"].get("authorization") in {"user", "local"}
            and value.get("inline") and (value.get("recovered_local")
                or (value.get("publication") or {}).get("parent_execution"))
            and all(value["actor"].get(key) == actor[key]
                    for key in ("server_url", "account_id", "resource")))
        if foreign and not transferable:
            continue
        if (value.get("publication") and not value["publication"].get("parent_execution")
                and api.requests.authorization != "user"):
            continue
        reservation = reserve_launch(api, value["task_id"])
        if reservation is None:
            continue
        try:
            if transferable:
                from simstack.server_api.http_transport import RunnerAPIError
                try:
                    response = await api.tasks.recover_local_launch(value)
                except RunnerAPIError as exc:
                    if exc.status_code in {401, 403, 404, 409}:
                        continue
                    raise
                # A different actor cannot retire the original owner's receipt.
                if response.get("state") != "claimed":
                    continue
            else:
                response = await reconcile_claim(api, value)
            if response.get("state") in {"pending", "publication_pending"}:
                continue
            if response.get("state") in {"active", "terminal", "stale", "rejected"}:
                finish(api, value, claimed=False)
                continue
            entry = response.get("task")
            if entry is None or response.get("state") not in {"claimed", "recoverable"}:
                continue
            if bool(getattr(entry, "_execution_local", False)) != (api.requests.authorization in {"user", "local"}):
                continue
            if str(entry.id) != value["task_id"]:
                raise ValueError(
                    "Recovered launch does not match its publication identity"
                )
            value = receipt(
                api,
                entry.id,
                begin=True,
                revision=getattr(entry, "repository_revision", value.get("revision")),
                generation=getattr(
                    entry, "_execution_generation", value.get("generation")
                ),
                claim_id=getattr(entry, "_execution_claim_id", value["claim_id"]),
                inline=value.get("inline", False),
                publication=None if transferable or response.get("recovered_local") else value.get("publication"),
                request_id=None if transferable or response.get("recovered_local") else value.get("request_id"),
                recovered_local=transferable or response.get("recovered_local", False) or value.get("recovered_local", False),
            )
            finish(api, value, claimed=True)
            if response.get("local_execution_token"):
                object.__setattr__(entry, "_execution_token", response["local_execution_token"])
            object.__setattr__(entry, "_launch_reservation", reservation)
            object.__setattr__(entry, "_launch_receipt", value)
            recovered.append(entry)
            reservation = None  # ownership passes to the executor, including its queue
        finally:
            if reservation is not None:
                reservation.close()
    if not recovered and observed is not None and getattr(observed, "_execution_recoverable_local", False):
        # Another host may never have seen the dead publisher's local journal.
        # The Server transfer marker and current execution are sufficient proof.
        reservation = reserve_launch(api, observed.id)
        if reservation is not None:
            try:
                from simstack.server_api.http_transport import RunnerAPIError
                try:
                    response = await api.tasks.recover_local_launch(api.tasks.execution(observed))
                except RunnerAPIError as exc:
                    if exc.status_code in {401, 403, 404, 409}:
                        # Another surviving consumer may own the handoff; this
                        # waiter retains its dependency without launching it.
                        return recovered
                    raise
                current = response.get("task")
                if response.get("state") == "claimed" and current is not None:
                    value = receipt(api, current.id, begin=True, inline=True, recovered_local=True,
                        revision=current.repository_revision,
                        generation=getattr(current, "_execution_generation", None),
                        claim_id=getattr(current, "_execution_claim_id", None))
                    finish(api, value, claimed=True)
                    object.__setattr__(current, "_execution_token", response["local_execution_token"])
                    object.__setattr__(current, "_launch_reservation", reservation)
                    object.__setattr__(current, "_launch_receipt", value)
                    recovered.append(current)
                    reservation = None
            finally:
                if reservation is not None:
                    reservation.close()
    if actor["authorization"] in {"user", "local"}:
        # A prestart claim rotation above can retire its old native observation
        # only after Server has fenced any late start of that old owner.
        await native_observations.recover(api, task_id=task_id)
    return recovered


def adopt_execution(execution):
    """Install Server's admitted incarnation within the current worker scope."""
    _execution.set(dict(execution))


def current_execution():
    value = _execution.get()
    if value:
        return value
    task_id = os.environ.get("SIMSTACK_EXECUTION_TASK")
    claim_id = os.environ.get("SIMSTACK_EXECUTION_CLAIM")
    if not task_id or not claim_id:
        return None
    value = {"task_id": task_id, "claim_id": claim_id}
    if os.environ.get("SIMSTACK_EXECUTION_GENERATION"):
        value["generation"] = os.environ["SIMSTACK_EXECUTION_GENERATION"]
    if os.environ.get("SIMSTACK_EXECUTION_START"):
        value["start_id"] = os.environ["SIMSTACK_EXECUTION_START"]
    return value


@contextmanager
def task_execution(entry):
    existing = current_execution()
    claim_id = getattr(entry, "_execution_claim_id", None)
    value = (
        {
            "task_id": str(entry.id),
            "claim_id": claim_id,
            "generation": getattr(entry, "_execution_generation", None),
        }
        if claim_id
        else None
    )
    if existing and existing.get("task_id") == str(entry.id):
        value = existing
    if value and "start_id" not in value:
        start_id = getattr(entry, "_execution_start_id", None) or str(uuid4())
        object.__setattr__(entry, "_execution_start_id", start_id)
        value = {**value, "start_id": start_id}
    token = _execution.set(value)
    try:
        yield
    finally:
        _execution.reset(token)
