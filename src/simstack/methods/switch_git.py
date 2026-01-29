from simstack.core.node import node
from simstack.models import StringData
from simstack.core.context import context

@node
def switch_git(branch: StringData, **kwargs) -> bool:
    node_runner = kwargs["node_runner"]

    project_root = context.project_root.resolve()
    cmd = f"git stash && git pull && git checkout {branch.value}"

    node_runner.run_command("git_switch", cmd, str(project_root))

    return node_runner.success

