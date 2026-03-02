
from pathlib import Path
from typing import List, TYPE_CHECKING
from simstack.core.resources import allowed_resources
from simstack.models.parameters import Resource
from simstack.models.resource_definition import ResourceDefinition, GitRepo
from simstack.util.transform_file_name import transform_file_name
from simstack.util.init_data_source import initialize_resource_from_db, initialize_paths_from_db
from simstack.util.toml_reader import TomlReader
from simstack.util.database_information import DatabaseInformation

if TYPE_CHECKING:
    from simstack.util.db import Database

class ConfigReader(DatabaseInformation):
    """
    Represents a configuration reader that integrates with a database and a resource
    definition system.
    """

    def __init__(
        self,
        db_info: DatabaseInformation,
        resource_definition: ResourceDefinition,
        *,
        project_root: Path,
        git_list: list[Path] | None = None,
    ):
        super().__init__(*db_info.get_information())

        self._project_root = project_root
        self._resource_definition = resource_definition
        self._git_list = list(git_list or [])
        self._resource_str = resource_definition.resource_str

    @classmethod
    async def create(
        cls,
        resource_str,
        db: "Database",
        toml_reader: TomlReader,
        project_root: Path,
        **kwargs,
    ):
        import logging
        logger = logging.getLogger("config-reader")  # do this here because the calling function sets the logger up

        required_keys = ["resource", "python_path", "ssh_key", "git_list", "allowed_resources",
                         "workdir", "environment_start"]
        init_done = False
        config = {"project_root": project_root}
        for key in required_keys:
            if key in kwargs:
                config[key] = kwargs.get(key)
                logger.info(f"Init from kwargs: {key}: {kwargs.get(key)}")
            else:
                init_done = False

        resource_definition = None
        git_list = []
        if not init_done:
            if not toml_reader:
                toml_reader = TomlReader(project_root)
            use_db_for_init = toml_reader.use_db()
            workdir_self = kwargs.get("workdir", None)
            if workdir_self is None:
                workdir_self = toml_reader.get("parameters.general.workdir_self", None)
                if workdir_self is None:
                    workdir_self = toml_reader.get("resources.self.workdir", None)
            if workdir_self is None:
                raise ValueError("No workdir for self specified in config file or keyword arguments.")
            else:
                workdir_self = Path(workdir_self)

            logger.info(f"toml-file read, use_db_for_init: {use_db_for_init}")
            if use_db_for_init:  # get all data from the simstack.toml file
                resource_definition = await initialize_resource_from_db(resource_str, db, workdir_self)
                await initialize_paths_from_db(db)
            else:
                allowed_resources_list = toml_reader.get_allowed_resources()
                allowed_resources.set_resources(allowed_resources_list)
                resource_definition = toml_reader.get_resource_definition(resource_str)
                git_list = toml_reader.get_git_list()
                toml_reader.build_routes()

        if resource_definition is None:
            raise ValueError("No valid resource definition found.")

        # override the values in resource definition with those from the keyword arguments
        for key in resource_definition.model_fields.keys():
            if key in config:
                logger.info(f"Overriding {key} from kwargs to: {config[key]}")
                resource_definition.__setattr__(key, config[key])
                del config[key]
                del required_keys[required_keys.index(key)]

        project_root = config.pop("project_root")
        git_list_final = config.pop("git_list", git_list)
        config.pop("resource")
        if config:
            logger.warning(f"Ignoring unused ConfigReader init keys: {sorted(config.keys())}")

        logger.info(f"ConfigReader initialized with workdir: {resource_definition.workdir}")
        for key, value in resource_definition.__dict__.items():
            logger.debug(f"resource_definition.{key}: {value}")

        return cls(db, resource_definition, project_root=project_root, git_list=git_list_final)

    @property
    def git_list(self) -> List[GitRepo]:
        return self._git_list

    @property
    def resource(self) -> Resource:
        return Resource(value=self._resource_str)

    @property
    def workdir(self) -> Path:
        return transform_file_name(self._resource_definition.workdir, self.project_root)

    @property
    def environment_start(self) -> str:
        return self._resource_definition.environment_start

    @property
    def python_paths(self) -> List[Path]:
        return [transform_file_name(p) for p in self._resource_definition.python_paths]

    @property
    def ssh_key(self) -> Path:
        return transform_file_name(self._resource_definition.ssh_key)

    @property
    def hostname(self):
        return self._resource_definition.hostname

    @resource.setter
    def resource(self, value: str):
        raise ValueError("ConfigReader: Resource cannot be set directly")

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def docker(self) -> bool:
        return False