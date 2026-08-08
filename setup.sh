#!/usr/bin/env bash
# setup.sh - Seamless setup for db-agent

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "=========================================="
echo "      db-agent Installation Wizard        "
echo "=========================================="

if ! command -v python3 &> /dev/null; then
    echo "[Error] Python 3 is required but not installed. Please install Python 3.12+ and try again."
    exit 1
fi

UV_BIN="$HOME/.local/bin/uv"
if ! command -v uv &> /dev/null && [ ! -f "$UV_BIN" ]; then
    echo "Installing uv (fast Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$HOME/.local/bin:$PATH"

echo "Creating virtual environment..."
if command -v uv &> /dev/null; then
    uv venv "$DIR/.venv"
    source "$DIR/.venv/bin/activate"
    uv pip install -e "$DIR"
else
    python3 -m venv "$DIR/.venv"
    source "$DIR/.venv/bin/activate"
    pip install -e "$DIR"
fi

echo "Linking db-agent command globally..."
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/db-agent" "$HOME/.local/bin/db-agent"

echo "=========================================="
echo " Setup complete!"
if [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
    echo " You can now run 'db-agent' from any folder in your terminal."
else
    echo " Linked to '~/.local/bin/db-agent'."
    echo " Note: Please ensure '~/.local/bin' is added to your PATH environment variable."
fi
echo " Configure a provider inside db-agent with /provider set <name>."
echo "=========================================="
