#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
P02_DIR="$PROJECT_ROOT/experiments/p02"
"${PYTHON:-python3.13}" -m venv "$P02_DIR/.venv"
"$P02_DIR/.venv/bin/python" -m pip install -r "$P02_DIR/requirements.txt"
"$P02_DIR/.venv/bin/python" -m pip check
