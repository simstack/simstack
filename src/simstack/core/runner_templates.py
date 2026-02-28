from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from typing import Dict, Any, List, Optional

# In this project structure, templates are kept in examples/templates
# instead of core code.
TEMPLATE_DIR = Path(__file__).parent.parent.parent.parent.parent / "examples" / "templates"

class RunnerTemplateManager:
    def __init__(self):
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            trim_blocks=True,
            lstrip_blocks=True
        )

    def render_script(self, resource: str, program_template: str, context: Dict[str, Any]) -> str:
        """
        Renders a shell script using a program-specific template that extends a resource-specific template.
        """
        # We need a dynamic extension: program_template should extend resource_{resource}.sh.j2
        # However, Jinja2 doesn't support dynamic 'extends' easily from strings unless we use a wrapper.
        # Another approach is to pass the resource_template_name to the program_template.
        
        context['resource_template_name'] = f"resource_{resource}.sh.j2"
        template = self.env.from_string(program_template)
        return template.render(**context)

    def render_from_file(self, resource: str, template_name: str, context: Dict[str, Any]) -> str:
        """
        Renders a script where the program logic is in a file that extends the base resource script.
        """
        context['resource_template_name'] = f"resource_{resource}.sh.j2"
        template = self.env.get_template(template_name)
        return template.render(**context)

class BaseRunner:
    """
    Base class for program runners that use Jinja2 templates.
    """
    def __init__(self, resource: str = "local"):
        self.resource = resource
        self.template_manager = RunnerTemplateManager()

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
            self.get_context()
        )
