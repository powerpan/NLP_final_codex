#!/bin/zsh
set -eu
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
cd "$PROJECT_DIR"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 "請先安裝：python3 -m venv .venv && .venv/bin/python -m pip install -e '.[terminal]'"
  exit 1
fi
exec "$PYTHON_BIN" -m maplatform shell "$@"
