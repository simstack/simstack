#!/usr/bin/env bash
# Optional scratch checkpoint. The normal Slurm path execs run_node directly.
scratch=$1
recovery=$2
shift 2
node_pid=

terminate_node() {
    # Ignore repeated wrapper signals while the owned child is being reaped.
    trap '' TERM INT USR1
    if [ -z "$node_pid" ]; then
        exit "$2"
    fi
    kill -s "$1" -- "-$node_pid" 2>/dev/null || true
    wait "$node_pid"
    node_status=$?
    # The main child may have exited without waiting for its own children.
    kill -KILL -- "-$node_pid" 2>/dev/null || true
    exit "$node_status"
}
trap 'terminate_node TERM 143' TERM
trap 'terminate_node INT 130' INT

checkpoint() {
    interrupted=1
    mkdir -p "$recovery" && cp -a "$scratch/." "$recovery"
}
trap checkpoint USR1

# A separate process group keeps forwarding confined to this execution.
set -m
"$@" &
node_pid=$!
set +m
while :; do
    interrupted=0
    wait "$node_pid"
    node_status=$?
    [ "$interrupted" -eq 0 ] && break
done
exit "$node_status"
