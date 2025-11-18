#!/bin/bash
set -euo pipefail
/app/git-sync.sh
# Ensure we run from the project root cloned into the image
cd "$REPO_DIR"

/app/make-toml.sh

# Make sure your package is discoverable
export PYTHONPATH="${REPO_DIR}/src:${REPO_DIR}/src:${PYTHONPATH:-}"

# Run the runner module inside the pixi "default" environment
exec pixi run --environment default --locked python -m simstack.core.runner "$@"
