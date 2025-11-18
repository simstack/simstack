#!/bin/bash
/app/git-sync.sh

cd "$REPO_DIR" || echo "no repo dir set" && exit

/app/make-toml.sh
# Make sure your package is discoverable
export PYTHONPATH="${REPO_DIR}/src:${REPO_DIR}/src:${PYTHONPATH:-}"

if [ $# -eq 0 ]; then
    # No arguments - start the default application
    echo "Starting SimStack Node with ID: ${NODE_ID:-default} and resource ${RESOURCE_ID:-local}"
    exec pixi run --environment default --locked python -m simstack.core.run_node --node-id "${NODE_ID:-default}" --resource "${RESOURCE_ID:-local}"
else
    # Arguments provided - execute them in pixi environment
    exec pixi run --environment default --locked "$@"
fi
