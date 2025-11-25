_SAMPLE_SIMSTACK_TOML_DICT = {
    "parameters": {
        "common": {
            "resources": [
                "local",
                "int-nano",
                "horeka",
                "justus",
                "self",
                "exchange",
                "uploads",
            ],
            "database": "wolfgang_data",
            "test_database": "wolfgang_test",
            "connection_string": "mongodb://localhost:27017/",
        },
        "local": {
            "ssh-key": "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh",
            "resource": "local",
            "workdir": "C:\\Users\\bj7610\\simstack",
            "python_path": [
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model",
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src",
            ],
        },
        "self": {
            "ssh-key": "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh",
            "resource": "local",
            "workdir": "C:\\Users\\bj7610\\simstack",
            "python_path": [
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model",
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src",
            ],
        },
        "uploads": {
            "ssh-key": "C:\\Users\\bj7610\\Documents\\etc\\.ssh\\surface11_openssh",
            "resource": "self",
            "workdir": "C:\\Users\\bj7610\\simstack",
            "python_path": [
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model",
                "C:\\Users\\bj7610\\PyCharmProjects\\simstack-model\\src",
            ],
        },
        "int-nano": {
            "ssh-key": "/home/ws/bj7610/.ssh/idrsa",
            "workdir": "/home/ws/bj7610/simstack",
            "python_path": [
                "/home/ws/bj7610/projects/simstack-model",
                "/home/ws/bj7610/projects/simstack-model/src",
            ],
            "environment_start": "conda activate simstack-env",
        },
    },
    "server": {
        "port": 8000,
        "secret_key": "secret_key",
        "upload_dir": "C:\\Users\\bj7610\\simstack\\uploads",
    },
    "hosts": {
        "local": "localhost",
        "int-nano": "int-nano.int.kit.edu",
        "justus": "justus.int.kit.edu",
        "horeka": "horeka.int.kit.edu",
    },
    "routes": [
        {"source": "local", "target": "int-nano", "host": "local"},
        {"source": "int-nano", "target": "local", "host": "local"},
        {"source": "horeka", "target": "local", "host": "horeka"},
        {"source": "local", "target": "horeka", "host": "horeka"},
        {"source": "justus", "target": "local", "host": "justus"},
        {"source": "local", "target": "justus", "host": "justus"},
    ],
    "paths": {
        "models": {"path": "src/simstack/models", "drops": "src"},
        "methods": {"path": "src/simstack/methods", "drops": "src"},
        "tests": {"path": "tests", "drops": "", "use_pickle": True},
    },
}


def sample_config_writer_entrypoint() -> None:
    """
    Entry point to write a sample simstack.toml configuration file.
    """
    import os
    import toml

    config_path = os.path.join(os.getcwd(), "simstack.toml")
    if os.path.exists(config_path):
        print(
            f"simstack.toml already exists at {config_path}. Aborting to avoid overwrite."
        )
        return

    with open(config_path, "w") as config_file:
        toml.dump(_SAMPLE_SIMSTACK_TOML_DICT, config_file)

    print(f"Sample simstack.toml written to {config_path}")
