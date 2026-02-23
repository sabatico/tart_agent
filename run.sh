#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/venv}"
RUN_DIR="${RUN_DIR:-$REPO_DIR/run}"
PID_FILE="${PID_FILE:-$RUN_DIR/tart_agent.pid}"
AUTO_STOP_EXISTING="${AUTO_STOP_EXISTING:-true}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-7000}"

cd "$REPO_DIR"
mkdir -p "$RUN_DIR"

is_agent_process() {
  local pid="$1"
  local cmd
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  [[ "$cmd" == *"python"*agent.py* || "$cmd" == *"gunicorn"*agent.py* ]]
}

stop_pid_if_running() {
  local pid="$1"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if ! is_agent_process "$pid"; then
    return 1
  fi

  echo "==> Stopping existing TART Agent process (pid=$pid)"
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  echo "==> Existing process did not stop gracefully; forcing kill"
  kill -9 "$pid" 2>/dev/null || true
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${existing_pid:-}" ]]; then
    if [[ "$AUTO_STOP_EXISTING" == "true" ]]; then
      stop_pid_if_running "$existing_pid" || true
    fi
  fi
  rm -f "$PID_FILE"
fi

port_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${port_pids:-}" ]]; then
  if [[ "$AUTO_STOP_EXISTING" == "true" ]]; then
    for pid in $port_pids; do
      if is_agent_process "$pid"; then
        stop_pid_if_running "$pid" || true
      fi
    done
  fi
fi

still_bound_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${still_bound_pid:-}" ]]; then
  echo "!! Port $PORT is already in use (pid(s): $still_bound_pid)."
  echo "   Set a different PORT, or free that port and retry."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "==> Ensuring dependencies are installed"
"$VENV_DIR/bin/pip" install -r requirements.txt

echo "==> Starting TART Agent on ${HOST}:${PORT}"
echo "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT
AGENT_PORT="$PORT" exec "$VENV_DIR/bin/python" agent.py
