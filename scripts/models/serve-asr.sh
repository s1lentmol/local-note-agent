#!/bin/sh
set -eu
: "${ASR_RUNTIME:?Set absolute path to existing whisper-cli}"
: "${ASR_MODEL:?Set absolute path to existing ggml model}"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT/backend"
exec "${PYTHON:-python3}" -m app.model_clients.asr_service
