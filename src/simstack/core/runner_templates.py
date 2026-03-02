from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from typing import Dict, Any, List, Optional
import tomllib  # For Python 3.11+

from simstack.models.parameters import Resource
import logging
logger = logging.getLogger("templates")

# In this project structure, templates are kept in examples/templates
# instead of core code.
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "examples" / "templates"
CONFIG_FILE = PROJECT_ROOT / "config.toml"

class ExecutorTemplateManager:
    def __init__(self):
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            trim_blocks=True,
            lstrip_blocks=True
        )
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "rb") as f:
                return tomllib.load(f)
        return {}

    def _get_os(self, resource: str, context: Dict[str, Any]) -> str:
        """
        Determines the target OS for the given resource.
        Defaults to 'linux'.
        """
        # 1. Check if 'os' is explicitly provided in context
        if 'os' in context:
            return context['os'].lower()
        
        # 2. Check if the resource configuration specifies an OS
        resource_config = self.config.get(resource, {})
        if 'os' in resource_config:
            return resource_config['os'].lower()
        
        # Default to linux
        return 'linux'

    def _get_shell(self, resource: str, context: Dict[str, Any]) -> str:
        """
        Determines the target shell for the given resource.
        Defaults to 'powershell' on Windows and 'bash' on Linux.
        """
        # 1. Check if 'shell' is explicitly provided in context
        if 'shell' in context:
            return context['shell'].lower()

        # 2. Check if the resource configuration specifies a shell
        resource_config = self.config.get(resource, {})
        if 'shell' in resource_config:
            return resource_config['shell'].lower()

        target_os = self._get_os(resource, context)
        if target_os == 'windows':
            return 'cmd'
        return 'bash'

    def render_script(self, resource: str, program_template: str, context: Dict[str, Any]) -> str:
        """
        Renders a shell script using a program-specific template that extends the base script.
        """
        target_os = self._get_os(resource, context)
        target_shell = self._get_shell(resource, context)

        if target_os == 'windows':
            if target_shell == 'cmd':
                context['resource_template_name'] = "base_script.cmd.j2"
            else:
                context['resource_template_name'] = "base_script.ps1.j2"
        else:
            context['resource_template_name'] = "base_script.sh.j2"
            
        context['config'] = self.config
        context['resource'] = resource
        template = self.env.from_string(program_template)
        return template.render(**context)

    def render_from_file(self, resource: str, template_name: str, context: Dict[str, Any], task_id: Optional[str] = None) -> str:
        """
        Renders a script where the program logic is in a file that extends the base resource script.
        """
        target_os = self._get_os(resource, context)
        target_shell = self._get_shell(resource, context)

        if target_os == 'windows':
            if target_shell == 'cmd':
                context['resource_template_name'] = "base_script.cmd.j2"
                # If the template_name ends with .sh.j2 or .ps1.j2, try to find .cmd.j2 equivalent
                if template_name.endswith(".sh.j2") or template_name.endswith(".ps1.j2"):
                    cmd_template_name = template_name.replace(".sh.j2", ".cmd.j2").replace(".ps1.j2", ".cmd.j2")
                    try:
                        self.env.get_template(cmd_template_name)
                        template_name = cmd_template_name
                    except Exception:
                        pass
            else:
                context['resource_template_name'] = "base_script.ps1.j2"
                # If the template_name ends with .sh.j2, try to find .ps1.j2 equivalent
                if template_name.endswith(".sh.j2"):
                    ps_template_name = template_name.replace(".sh.j2", ".ps1.j2")
                    # Check if the ps1 template exists
                    try:
                        self.env.get_template(ps_template_name)
                        template_name = ps_template_name
                    except Exception:
                        # Fallback to original template if ps1 version doesn't exist
                        pass
        else:
            context['resource_template_name'] = "base_script.sh.j2"
            
        context['config'] = self.config
        context['resource'] = resource
        context['task_id'] = task_id
        template = self.env.get_template(template_name)
        return template.render(**context)

class BaseExecutor:
    """
    Base class for program runners that use Jinja2 templates.
    """
    def __init__(self, resource: str | Resource = "local", task_id: Optional[str] = None):
        if isinstance(resource, Resource):
            self.resource = str(resource)
        elif isinstance(resource, str):
            self.resource = resource
        else:
            raise TypeError("resource must be a string or Resource object")
        if self.resource == "self":
            self.resource = "local"
        self.task_id = task_id
        self.template_manager = ExecutorTemplateManager()

    def get_context(self) -> Dict[str, Any]:
        """
        Override this to provide program-specific context.
        """
        return {}

    def get_template_name(self) -> str:
        """
        Override this to return the name of the program-specific template file.
        """
        raise NotImplementedError

    def render(self) -> str:
        return self.template_manager.render_from_file(
            self.resource, 
            self.get_template_name(), 
            self.get_context(),
            task_id=self.task_id
        )


class ProgramExecutor(BaseExecutor):
    """
    Generic runner that can be configured with program-specific info.
    """
    def __init__(self, resource: str | Resource = "local",
                 environment_modules: List[str] = None,
                 program_env: Dict[str, str] = None,
                 input_files: List[str] = None,
                 output_files: List[str] = None,
                 run_command: str = "",
                 scripts: List[str] = None,
                 program_name: str = None,
                 task_id: Optional[str] = None):
        super().__init__(resource, task_id=task_id)
        
        # Load defaults from config if program_name is provided

        config = self.template_manager.config
        logger.info(f"task_id: {task_id} Program: {program_name} Resource: {self.resource}")
        logger.info(f"task_id: {task_id} Config: {config}")
        if program_name and config:
            # Try to find program in the resource-specific program dict
            prog_config = config.get(self.resource, {}).get("program", {}).get(program_name)
            logger.info(f"task_id: {task_id}  Found prog_config:  {prog_config}")
            if not prog_config:
                # Fallback to top-level program_specifics for backward compatibility if it exists
                prog_config = config.get("program_specifics", {}).get(program_name)
            
            if not prog_config:
                raise ValueError(f"Program '{program_name}' not found for resource '{self.resource}' in config.")
                
            environment_modules = environment_modules or prog_config.get("environment_modules")
            program_env = program_env or prog_config.get("program_env")
            input_files = input_files or prog_config.get("input_files")
            output_files = output_files or prog_config.get("output_files")
            run_command = run_command or prog_config.get("run_command")
            scripts = scripts or prog_config.get("scripts")
            self.use_tmp = prog_config.get("use_tmp", True) 
        elif program_name:
            raise ValueError(f"Program '{program_name}' not found in config for resource '{self.resource}'")

        self.environment_modules = environment_modules or []
        self.program_env = program_env or {}
        self.input_files = input_files or []
        self.output_files = output_files or []
        self.run_command = run_command
        self.scripts = scripts or []
        # If use_tmp wasn't set from config, default it to True
        if not hasattr(self, 'use_tmp'):
            self.use_tmp = True

    def get_template_name(self) -> str:
        return "generic_program.sh.j2"

    def get_context(self) -> Dict[str, Any]:
        return {
            "environment_modules": self.environment_modules,
            "program_env": self.program_env,
            "input_files": self.input_files,
            "output_files": self.output_files,
            "run_command": self.run_command,
            "use_tmp": self.use_tmp,
            "scripts": self.scripts
        }

    def run_script(self):
        """Compatibility method for old JustusRunner"""
        return self.render()
