#!/bin/bash
# Periodically check for Git changes, pull, and restart the runner
set -euo pipefail

# Configuration
: "${APP_DIR:=/app/simstack-model}"
: "${GIT_REMOTE:=origin}"
: "${GIT_REF:=main}"                  # Branch/tag to track
: "${CHECK_INTERVAL:=60}"             # Seconds between checks
: "${GRACEFUL_TIMEOUT:=30}"           # Seconds to wait before force-kill on restart

cd "$APP_DIR"
git config --global --add safe.directory "$APP_DIR"

# Ensure the working copy exists and has a remote set
if [ ! -d ".git" ]; then
  echo "[runner] ERROR: $APP_DIR is not a git repository."
  exit 1
fi

# Determine the ref to track
detect_ref() {
  # Prefer configured GIT_REF; fallback to current branch or HEAD
  local branch
  branch="$GIT_REF"
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  fi
  echo "$branch"
}

# Return 0 if a remote update is available, 1 otherwise
update_available() {
  local branch remote_ref
  branch="$(detect_ref)"
  git fetch --quiet --prune --tags "$GIT_REMOTE" || return 1
  remote_ref="$GIT_REMOTE/$branch"

  if git rev-parse --verify -q "$remote_ref" >/dev/null; then
    # Count how many commits HEAD is behind remote
    local behind
    behind="$(git rev-list --count "HEAD..$remote_ref" 2>/dev/null || echo 0)"
    if [ "${behind:-0}" -gt 0 ]; then
      return 0
    fi
    # Also detect tag updates that move the same name
    if ! git diff --quiet --no-ext-diff "HEAD" "$remote_ref"; then
      return 0
    fi
    return 1
  else
    # No matching remote branch; compare against FETCH_HEAD as a fallback
    if ! git diff --quiet --no-ext-diff "HEAD" "FETCH_HEAD"; then
      return 0
    fi
    return 1
  fi
}

pull_updates() {
  local branch remote_ref
  branch="$(detect_ref)"
  remote_ref="$GIT_REMOTE/$branch"

  # Try a safe fast-forward first; fall back to hard reset if necessary
  if git merge-base --is-ancestor HEAD "$remote_ref" 2>/dev/null; then
    echo "[runner] Fast-forwarding to $remote_ref"
    git -c advice.detachedHead=false checkout -q "$branch" || true
    git pull --ff-only "$GIT_REMOTE" "$branch" || true
  else
    echo "[runner] Non fast-forward; hard resetting to $remote_ref"
    git fetch --quiet "$GIT_REMOTE" "$branch" || true
    git -c advice.detachedHead=false checkout -q "$branch" || true
    git reset --hard "$remote_ref" || true
  fi
}

child_pid=""

start_runner() {
  echo "[runner] Starting runner process..."
  # Make sure the package is importable
  export PYTHONPATH="$APP_DIR/src:$APP_DIR:${PYTHONPATH:-}"
  # Start in background so we can supervise and restart it
  pixi run --environment default --locked python -m simstack.core.runner "$@" &
  child_pid=$!
  echo "[runner] Runner PID: $child_pid"
}

stop_runner() {
  if [ -n "${child_pid:-}" ] && kill -0 "$child_pid" 2>/dev/null; then
    echo "[runner] Stopping runner PID $child_pid ..."
    kill -TERM "$child_pid" 2>/dev/null || true

    # Wait up to GRACEFUL_TIMEOUT seconds for graceful shutdown
    local waited=0
    while kill -0 "$child_pid" 2>/dev/null; do
      if [ "$waited" -ge "$GRACEFUL_TIMEOUT" ]; then
        echo "[runner] Force killing runner PID $child_pid"
        kill -KILL "$child_pid" 2>/dev/null || true
        break
      fi
      sleep 1
      waited=$((waited+1))
    done
    wait "$child_pid" 2>/dev/null || true
  fi
  child_pid=""
}

forward_signal() {
  local sig="$1"
  if [ -n "${child_pid:-}" ] && kill -0 "$child_pid" 2>/dev/null; then
    kill "-$sig" "$child_pid" 2>/dev/null || true
  fi
}

# Forward termination signals to child and exit cleanly
trap 'forward_signal TERM; stop_runner; exit 143' TERM
trap 'forward_signal INT;  stop_runner; exit 130' INT

# Kick off the runner
start_runner "$@"

# Supervisor loop
while true; do
  # Poll child status every second and also check git at the configured interval
  for ((i=0; i< CHECK_INTERVAL; i++)); do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      # Child exited; mirror its exit code
      wait "$child_pid" 2>/dev/null
      exit_code=$?
      echo "[runner] Runner exited with code $exit_code"
      exit "$exit_code"
    fi
    sleep 1
  done

  # Time to check for updates
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[runner] [$ts] Checking for git updates..."
  if update_available; then
    echo "[runner] Updates detected. Pulling and restarting runner..."
    pull_updates || echo "[runner] WARN: Pull/reset encountered issues; continuing."
    stop_runner
    start_runner "$@"
  else
    echo "[runner] No updates found."
  fi
done
