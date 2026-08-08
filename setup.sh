#!/usr/bin/env bash
# setup.sh - Seamless setup for db-agent

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[Error] Python 3.12+ is required but was not found."
    exit 1
fi

if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
    echo "[Error] Python 3.12+ is required."
    exit 1
fi

VENV_DIR="$DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "Using existing virtual environment: $VENV_DIR"
fi

echo "Installing db-agent dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$DIR"
"$VENV_PYTHON" -c 'from mcp.server.fastmcp import FastMCP; print("MCP compatibility check passed")'

echo "Setup complete. Configure a provider with /provider set <name>."
