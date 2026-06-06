#!/bin/bash
set -euo pipefail

# Configuration via env vars
#: "${GIT_REPO:?Set GIT_REPO, e.g. https://gitlab.example.com/ns/project.git}"
#: "${GIT_REF:=main}"                       # branch, tag, or commit
#: "${REPO_DIR:=/home/appuser/simstack/simstack-model}"        # target checkout directory (bind-mount this for persistence)
: "${SUBMODULES:=false}"                   # true to init/update submodules
# If using HTTPS tokens:
: "${GIT_USER:=oauth2}"
: "${GIT_TOKEN:=}"                         # CI_JOB_TOKEN, PAT, or empty
# If using SSH with BuildKit/agent, set GIT_SSH=true and ensure credentials are available
: "${GIT_SSH:=false}"

mkdir -p "$REPO_DIR"
git config --global --add safe.directory "$REPO_DIR"

# Build remote URL depending on auth method
if [ "$GIT_SSH" = "true" ]; then
  REMOTE="$GIT_REPO"  # e.g. git@gitlab.example.com:ns/project.git
else
  # HTTPS; inject token if provided
  if [ -n "$GIT_TOKEN" ]; then
    REMOTE="https://${GIT_USER}:${GIT_TOKEN}@${GIT_REPO#https://}"
  else
    REMOTE="$GIT_REPO"
  fi
fi

echo "checking out ${GIT_BRANCH}"
# Clone or update
if [ -d "$REPO_DIR/.git" ]; then
  echo "[git-sync] Updating existing repo at $REPO_DIR"
  git -C "$REPO_DIR" remote set-url origin "$REMOTE"
  git -C "$REPO_DIR" config --get-all remote.origin.fetch

  # Set to fetch all branches
  git -C "$REPO_DIR" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'

  # Refresh tracking branches
  git -C "$REPO_DIR" fetch --prune --tags

  git -C "$REPO_DIR" checkout -B "$GIT_BRANCH" "origin/$GIT_BRANCH" # || git -C "$REPO_DIR" checkout -q -B "$GIT_BRANCH" "origin/$GIT_BRANCH" || true
  git -C "$REPO_DIR" reset --hard "origin/$GIT_BRANCH" 2>/dev/null || true
else
  echo "[git-sync] Cloning $REMOTE -> $REPO_DIR"
  rm -rf "$REPO_DIR"/*
  git clone --depth 1 --branch "$GIT_BRANCH" "$REMOTE" "$REPO_DIR"
fi

# Optional submodules
if [ "$SUBMODULES" = "true" ]; then
  git -C "$REPO_DIR" submodule update --init --recursive --depth 1
fi

echo "[git-sync] Repo ready at $REPO_DIR (branch: $GIT_BRANCH)"
