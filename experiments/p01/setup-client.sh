#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ ! -x experiments/p01/.venv/bin/python ]; then
  "${PYTHON:-/opt/homebrew/bin/python3.13}" -m venv experiments/p01/.venv
fi
PIP_DISABLE_PIP_VERSION_CHECK=1 experiments/p01/.venv/bin/python -m pip install -r experiments/p01/requirements.txt
