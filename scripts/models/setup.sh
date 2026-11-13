#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MODEL_SCRIPTS_DIR="$PROJECT_ROOT/scripts/models"
. "$MODEL_SCRIPTS_DIR/common.sh"
configure_ollama "$PROJECT_ROOT"

runtime_valid() {
  [ -x "$OLLAMA_RUNTIME_DIR/ollama" ] && [ -x "$OLLAMA_RUNTIME_DIR/llama-server" ] &&
    (cd "$OLLAMA_RUNTIME_DIR" && shasum -a 256 -c "$MODEL_SCRIPTS_DIR/runtime.sha256" >/dev/null 2>&1)
}
if runtime_valid; then
  echo "Ollama 0.40.16 verified: $OLLAMA_RUNTIME_DIR; setup skipped (no download/extraction)."
  exit 0
fi

ARCHIVE="$PROJECT_ROOT/local/ollama/ollama-darwin.tgz"
ARCHIVE_SHA=a743717494315da7cd352982b805f6b992d4b4452ca117fd5fbf86c4ee46fca0
archive_valid() {
  [ -f "$1" ] && printf '%s  %s\n' "$ARCHIVE_SHA" "$1" | shasum -a 256 -c - >/dev/null 2>&1
}
mkdir -p "$(dirname "$ARCHIVE")" "$OLLAMA_RUNTIME_DIR"
if ! archive_valid "$ARCHIVE"; then
  curl -fL --retry 2 \
    https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-darwin.tgz \
    -o "$ARCHIVE.download"
  archive_valid "$ARCHIVE.download" || { echo 'Archive checksum mismatch.' >&2; exit 1; }
  mv "$ARCHIVE.download" "$ARCHIVE"
fi
tar -xzf "$ARCHIVE" -C "$OLLAMA_RUNTIME_DIR"
runtime_valid || { echo 'Runtime verification failed.' >&2; exit 1; }
echo "Ollama 0.40.16 installed: $OLLAMA_RUNTIME_DIR"
# Model pulls belong to the operator; setup never downloads or duplicates weights.
