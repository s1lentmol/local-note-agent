#!/bin/sh
# Sourced by setup/serve; the caller supplies the absolute project root.
configure_ollama() {
  OLLAMA_RUNTIME_DIR=${OLLAMA_RUNTIME_DIR:-$1/local/ollama/runtime}
  OLLAMA_MODELS=${OLLAMA_MODELS:-$1/local/ollama/models}
  OLLAMA_LOG_DIR=${OLLAMA_LOG_DIR:-$1/local/ollama/logs}
  OLLAMA_HOST=${OLLAMA_HOST:-127.0.0.1:11435}
  # Relative paths are always relative to the project root, regardless of cwd.
  case "$OLLAMA_RUNTIME_DIR" in /*) ;; *) OLLAMA_RUNTIME_DIR="$1/$OLLAMA_RUNTIME_DIR" ;; esac
  case "$OLLAMA_MODELS" in /*) ;; *) OLLAMA_MODELS="$1/$OLLAMA_MODELS" ;; esac
  case "$OLLAMA_LOG_DIR" in /*) ;; *) OLLAMA_LOG_DIR="$1/$OLLAMA_LOG_DIR" ;; esac
  case "${OLLAMA_HOST%:*}" in
    127.0.0.1|localhost|\[::1\]) ;;
    *) echo 'OLLAMA_HOST must be a loopback host:port (127.0.0.1, localhost, [::1]).' >&2; return 1 ;;
  esac
  OLLAMA_PORT=${OLLAMA_HOST##*:}
  case "$OLLAMA_PORT" in ''|*[!0-9]*) echo 'Invalid port.' >&2; return 1 ;; esac
  if [ "${#OLLAMA_PORT}" -gt 5 ] || [ "$OLLAMA_PORT" -lt 1 ] || [ "$OLLAMA_PORT" -gt 65535 ]; then
    echo 'Port must be between 1 and 65535.' >&2
    return 1
  fi
  export OLLAMA_RUNTIME_DIR OLLAMA_MODELS OLLAMA_LOG_DIR OLLAMA_HOST
}
