#!/usr/bin/env bash
# Optional scratch checkpoint. The normal Slurm path execs run_node directly.
if [[ $1 == --tmp-base ]]; then
    shift
    # Resolve on the compute node, with the same Core and environment as science.
    base=$("$3" -c 'import sys; from simstack.util.resource_config import resolve_tmp_base_dir; print(resolve_tmp_base_dir(sys.argv[1]).resolve())' "$1") || exit 1
    scratch="$base/${SIMSTACK_SLURM_SUBMISSION_TASK:?}"
else
    # Already submitted scripts pass a resolved task directory.
    scratch=$1
fi
recovery=$2
shift 2
if [[ -n ${SLURM_ARRAY_TASK_ID:-} && -n ${SIMSTACK_SLURM_SUBMISSION:-} ]]; then
    member=$("$1" -c 'import sys; from simstack.util.slurm_array import element_id; print(element_id(sys.argv[1], sys.argv[2]))' \
        "$SIMSTACK_SLURM_SUBMISSION" "$SLURM_ARRAY_TASK_ID") || exit 1
    scratch="$(dirname "$scratch")/$member"
    recovery="$(dirname "$(dirname "$recovery")")/$member/recovery"
fi
mkdir -p "$scratch" || exit 1
export SIMSTACK_SLURM_TASK_SCRATCH="$scratch"
node_pid=

terminate_node() {
    # Ignore repeated wrapper signals while the owned child is being reaped.
    trap '' TERM INT USR1 USR2
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
trap checkpoint USR1 USR2

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
