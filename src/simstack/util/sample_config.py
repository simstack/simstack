"""Write a complete API-only setup example without a TOML writer dependency."""

_SAMPLE_SIMSTACK_TOML = r'''
# API-only Runner configuration; MongoDB is configured only on Server.

[parameters]

[parameters.common]
resources = ["local", "int-nano", "horeka", "justus", "self", "exchange", "uploads"]

[parameters.local]
ssh-key = "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh"
resource = "local"
workdir = "C:\\Users\\bj7610\\simstack"
python_path = ["C:\\Users\\bj7610\\PyCharmProjects\\simstack-model", "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src"]

[parameters.self]
ssh-key = "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh"
resource = "local"
workdir = "C:\\Users\\bj7610\\simstack"
python_path = ["C:\\Users\\bj7610\\PyCharmProjects\\simstack-model", "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src"]

[parameters.uploads]
ssh-key = "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh"
resource = "self"
workdir = "C:\\Users\\bj7610\\simstack"
python_path = ["C:\\Users\\bj7610\\PyCharmProjects\\simstack-model", "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src"]

[parameters.int-nano]
ssh-key = "/home/ws/bj7610/.ssh/idrsa"
workdir = "/home/ws/bj7610/simstack"
python_path = ["/home/ws/bj7610/projects/simstack-model", "/home/ws/bj7610/projects/simstack-model/src"]
environment_start = "conda activate simstack-env"

[parameters.server]
url = "https://your-simstack-server.example/api"
token = "RESOURCE_TOKEN"

[hosts]
local = "localhost"
int-nano = "int-nano.int.kit.edu"
justus = "justus.int.kit.edu"
horeka = "horeka.int.kit.edu"

[[routes]]
source = "local"
target = "int-nano"
host = "local"

[[routes]]
source = "int-nano"
target = "local"
host = "local"

[[routes]]
source = "horeka"
target = "local"
host = "horeka"

[[routes]]
source = "local"
target = "horeka"
host = "horeka"

[[routes]]
source = "justus"
target = "local"
host = "justus"

[[routes]]
source = "local"
target = "justus"
host = "justus"

[paths]

[paths.models]
path = "src/simstack/models"
drops = "src"

[paths.methods]
path = "src/simstack/methods"
drops = "src"

[paths.tests]
path = "tests"
drops = ""
use_pickle = true
'''.lstrip()


def sample_config_writer_entrypoint() -> None:
    """Write the sample once, preserving existing machine configuration."""
    import os
    from pathlib import Path

    config_path = Path.cwd() / "simstack.toml"
    try:
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"simstack.toml already exists at {config_path}. Aborting to avoid overwrite.")
        return
    with os.fdopen(descriptor, "w") as config_file:
        config_file.write(_SAMPLE_SIMSTACK_TOML)
    print(f"Sample simstack.toml written to {config_path}")
