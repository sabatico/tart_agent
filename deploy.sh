#!/usr/bin/env bash

set -euo pipefail

# Deploy helper for TART agent node.
# - Pull latest code
# - Install/update Python dependencies
# - Optionally restart a service

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
RESTART_CMD="${RESTART_CMD:-}"

echo "==> Deploying TART Agent from $REMOTE/$BRANCH"
cd "$REPO_DIR"

echo "==> Fetch + fast-forward pull"
git fetch "$REMOTE" "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "!! Virtualenv not found at $VENV_DIR"
  echo "   Create it first: python3 -m venv \"$VENV_DIR\""
  exit 1
fi

echo "==> Installing/updating Python dependencies"
"$VENV_DIR/bin/pip" install -r requirements.txt

if [[ -n "$RESTART_CMD" ]]; then
  echo "==> Restarting service"
  eval "$RESTART_CMD"
else
  echo "==> No restart command configured."
  echo "   Set RESTART_CMD to restart your service, e.g.:"
  echo "   RESTART_CMD='sudo launchctl kickstart -k system/com.tart-agent'"
fi

echo "==> Deploy complete"
