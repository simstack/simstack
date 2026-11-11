"""Resolved execution settings returned by the Server bootstrap."""
from pathlib import Path
from simstack.models.parameters import Resource
from simstack.util.transform_file_name import transform_file_name

class ConfigReader:
    def __init__(self, resource_definition, *, project_root, server_url, server_token, account_id):
        self._resource_definition = resource_definition
        self._project_root = Path(project_root)
        self._server_url = server_url
        self._server_token = server_token
        self.account_id = account_id

    @property
    def server_url(self):
        return self._server_url

    @property
    def server_token(self):
        return self._server_token

    @property
    def resource(self):
        return Resource(value=self._resource_definition.resource_str)

    @property
    def workdir(self):
        return transform_file_name(self._resource_definition.workdir, self.project_root)

    @property
    def project_root(self):
        return self._project_root

    @property
    def environment_start(self):
        return self._resource_definition.environment_start

    @property
    def python_paths(self):
        return [transform_file_name(path, self.project_root) for path in self._resource_definition.python_paths]

    @property
    def ssh_key(self):
        return transform_file_name(self._resource_definition.ssh_key)

    @property
    def hostname(self):
        return self._resource_definition.hostname
