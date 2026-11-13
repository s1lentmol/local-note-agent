#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
. "$PROJECT_ROOT/scripts/models/common.sh"
configure_ollama "$PROJECT_ROOT"
if lsof -nP -iTCP:"$OLLAMA_PORT" -sTCP:LISTEN; then
  echo "Port $OLLAMA_PORT is occupied; refusing to start." >&2
  exit 1
fi
[ -x "$OLLAMA_RUNTIME_DIR/ollama" ] || { echo 'Run scripts/models/setup.sh first.' >&2; exit 1; }
mkdir -p "$OLLAMA_MODELS" "$OLLAMA_LOG_DIR"
export OLLAMA_CONTEXT_LENGTH=8192
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE=10m
export OLLAMA_NO_CLOUD=1
echo "Starting Ollama at $OLLAMA_HOST; logs: $OLLAMA_LOG_DIR/server.log; stop with Ctrl-C."
exec "$OLLAMA_RUNTIME_DIR/ollama" serve >>"$OLLAMA_LOG_DIR/server.log" 2>&1
