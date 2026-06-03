#!/usr/bin/env bash
# Runner-backed smoke bootstrap for the current simstack-only E2E.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="${SIMSTACK_PROJECT_ROOT:-$(cd "${script_dir}/../../.." && pwd)}"
workdir_self="${project_root}/simstack"
db_name="${SIMSTACK_TEST_DB:?SIMSTACK_TEST_DB must be set}"
connection_string="${SIMSTACK_TEST_DB_CONNECTION_STRING:?SIMSTACK_TEST_DB_CONNECTION_STRING must be set}"
config_path="${project_root}/simstack.toml"
backup_path=""
python_bin="${project_root}/.venv/bin/python"
create_model_table_bin="${project_root}/.venv/bin/create_model_table"
create_node_table_bin="${project_root}/.venv/bin/create_node_table"

export SIMSTACK_PROJECT_ROOT="${project_root}"
export SIMSTACK_TEST_DB="${db_name}"
export SIMSTACK_TEST_DB_CONNECTION_STRING="${connection_string}"

if [ ! -f "${project_root}/pyproject.toml" ] || [ ! -d "${project_root}/src/simstack" ]; then
  echo "SIMSTACK_PROJECT_ROOT must point to the simstack repository root. Got: ${project_root}" >&2
  exit 1
fi

if [ ! -x "${python_bin}" ] || [ ! -x "${create_model_table_bin}" ] || [ ! -x "${create_node_table_bin}" ]; then
  echo "Expected runner smoke tooling in ${project_root}/.venv/bin." >&2
  exit 1
fi

mkdir -p "${workdir_self}"

restore_config() {
  if [ -n "${backup_path}" ] && [ -f "${backup_path}" ]; then
    mv "${backup_path}" "${config_path}"
  else
    rm -f "${config_path}"
  fi

  # The runner writes its PID under test_workdir for single-runner protection.
  rm -f "${project_root}/test_workdir/runner_test.pid"

  # create_node_table currently writes node_models.txt into the repo root.
  rm -f "${project_root}/node_models.txt"
}

trap restore_config EXIT

if [ -f "${config_path}" ]; then
  backup_path="${config_path}.runner-smoke.bak"
  cp "${config_path}" "${backup_path}"
fi

# Keep parent bootstrap and child runner on the same test TOML shape.
"${python_bin}" -c "from pathlib import Path; from tests.with_context.with_runner.runner_smoke_toml import write_runner_smoke_toml; write_runner_smoke_toml(Path('${config_path}'), project_root=Path('${project_root}'), workdir_self=Path('${workdir_self}'), connection_string='${connection_string}', database_name='${db_name}', use_db=True)"

# The runner resolves submitted nodes through the mapping tables.
"${create_model_table_bin}" --dir tests
"${create_node_table_bin}" --dir tests

"${python_bin}" -m pytest -s -m runner_smoke \
  tests/without_context \
  tests/with_context/with_runner \
  --junitxml=pytest.xml \
  --cov-report=term-missing \
  --cov=simstack \
  --cov-config=pyproject.toml
