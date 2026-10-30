"""Motor-shaped HTTP adapter used beneath the existing ODMantic engine.

ODMantic still builds its own reference lookups and dirty-field updates. Only
those database operations cross HTTP; model hooks remain in the runner process.
"""

from __future__ import annotations

from types import SimpleNamespace

from simstack.runner_api.transport import RunnerAPIClient


class _OrderedWrites:
    """Implicit ODMantic save context; this is not a database transaction."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def start_transaction(self, *_args, **_kwargs):
        raise RuntimeError(
            "Runner database transactions must be implemented by a server API operation"
        )


class HTTPMotorCursor:
    def __init__(self, collection, operation, args, kwargs):
        self.collection = collection
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self._documents = None
        self._offset = 0

    async def to_list(self, length=None):
        if self._documents is None:
            self._documents = await self.collection._call(
                self.operation, *self.args, **self.kwargs
            )
        if length is None:
            result = self._documents[self._offset :]
            self._offset = len(self._documents)
            return result
        result = self._documents[self._offset : self._offset + length]
        self._offset += len(result)
        return result

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self.to_list(1)
        if not chunk:
            raise StopAsyncIteration
        return chunk[0]


class HTTPMotorCollection:
    def __init__(self, database, name):
        self.database = database
        self.name = name

    async def _call(self, operation, *args, **kwargs):
        session = kwargs.pop("session", None)
        if session is not None and not isinstance(session, _OrderedWrites):
            raise ValueError("Explicit database sessions are not supported on runners")
        return await self.database.client.api.persistence(
            self.name, operation, args, kwargs
        )

    def aggregate(self, pipeline, **kwargs):
        return HTTPMotorCursor(self, "aggregate", (pipeline,), kwargs)

    def find(self, *args, **kwargs):
        return HTTPMotorCursor(self, "find", args, kwargs)

    async def find_one(self, *args, **kwargs):
        return await self._call("find_one", *args, **kwargs)

    async def find_one_and_update(self, *args, **kwargs):
        return await self._call("find_one_and_update", *args, **kwargs)

    async def update_one(self, *args, **kwargs):
        return SimpleNamespace(**await self._call("update_one", *args, **kwargs))

    async def replace_one(self, *args, **kwargs):
        return SimpleNamespace(**await self._call("replace_one", *args, **kwargs))

    async def insert_one(self, *args, **kwargs):
        return SimpleNamespace(**await self._call("insert_one", *args, **kwargs))

    async def delete_many(self, *args, **kwargs):
        return SimpleNamespace(**await self._call("delete_many", *args, **kwargs))

    async def delete_one(self, *args, **kwargs):
        return SimpleNamespace(**await self._call("delete_one", *args, **kwargs))

    async def count_documents(self, *args, **kwargs):
        return await self._call("count_documents", *args, **kwargs)


class HTTPMotorDatabase:
    # This is a local placeholder required by ODMantic, never a server DB name.
    name = "runner"

    def __init__(self, client):
        self.client = client

    def __getitem__(self, name):
        return HTTPMotorCollection(self, name)


class HTTPMotorClient:
    """Contains no URI, credentials, sockets or connection to MongoDB."""

    def __init__(self, api: RunnerAPIClient):
        self.api = api
        self._database = HTTPMotorDatabase(self)

    def __getitem__(self, _name):
        return self._database

    async def start_session(self):
        return _OrderedWrites()

    def close(self):
        pass
