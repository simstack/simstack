"""Write a complete API-only setup example without a TOML writer dependency."""

_SAMPLE_SIMSTACK_TOML = '''# Enrollment starts the Runner. For a manual foreground launch after it stops,
# replace <slug> with the directory name from the installer's runner.log path.
# runner_dir="$HOME/.local/share/simstack/runners/<slug>"
# "$runner_dir/bin/simstack_runner" --credentials "$runner_dir/credentials.json" --project-root "$runner_dir/runtime" --config "$PWD/simstack.toml"
# The enrollment file supplies the token and URL. Update the fallback URL for your site.
[parameters.server]
url = "https://simstack.example.org/api"

# Use the same resource name as in the UI. It receives its workdir from Server.
[workstation.program.python]
run = "python --version"
'''


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
