from pathlib import Path


def write_runner_smoke_toml(
    config_path: Path,
    *,
    project_root: Path,
    workdir_self: Path,
    connection_string: str,
    database_name: str,
    use_db: bool,
) -> None:
    # The smoke wrapper and the child runner use the same test TOML shape.
    # The smoke flow has two separate process entry points:
    # 1. the parent bootstrap path, which still reads a temporary simstack.toml
    # 2. the child runner process, which reads runner-test.simstack.toml
    # Both files must describe the same resource/db contract, so this helper keeps
    # that one test-specific TOML shape in one place.
    config_path.write_text(
        "\n".join(
            [
                "[parameters]",
                "[parameters.general]",
                f"use_db = {str(use_db).lower()}",
                f'workdir_self = "{workdir_self}"',
                "[parameters.db]",
                f'database = "{database_name}"',
                f'connection_string = "{connection_string}"',
                "[resources]",
                'allowed_resources = ["self", "test"]',
                "[resources.self]",
                f'workdir = "{workdir_self}"',
                f'python_paths = ["{project_root / "src"}", "{project_root / "tests"}"]',
                'hostname = "localhost"',
                'environment_start = ""',
                "[resources.test]",
                f'workdir = "{workdir_self}"',
                f'python_paths = ["{project_root / "src"}", "{project_root / "tests"}"]',
                'hostname = "localhost"',
                'environment_start = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )
