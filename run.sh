#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-7000}"

cd "$REPO_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "==> Ensuring dependencies are installed"
"$VENV_DIR/bin/pip" install -r requirements.txt

echo "==> Starting TART Agent on ${HOST}:${PORT}"
AGENT_PORT="$PORT" exec "$VENV_DIR/bin/python" agent.py
