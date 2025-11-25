import datetime

from odmantic import Model, ObjectId

from simstack.models.parameters import Resource


class SlurmInfo(Model):
    node_registry: ObjectId
    updated: datetime.datetime
    resource: Resource
    job_id: str
    name: str
    user: str
    code: str
    time: str
    nodes: list[str]
