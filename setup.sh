#!/usr/bin/env bash
# setup.sh - Seamless installer for db-agent

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "=========================================="
echo "      db-agent Installation Wizard        "
echo "=========================================="

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo "[Error] Python 3 is required but not installed. Please install Python 3.12+ and try again."
    exit 1
fi

# 2. Check/Install Ollama
if ! command -v ollama &> /dev/null; then
    echo "Ollama is not installed. Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "Ollama is already installed."
fi

# 3. Start Ollama service if not running
if ! pgrep -x "ollama" > /dev/null; then
    echo "Starting Ollama service..."
    if command -v systemctl &> /dev/null; then
        sudo systemctl start ollama || true
    else
        ollama serve > /dev/null 2>&1 &
        sleep 3
    fi
fi

# 4. Pull Qwen model
echo "Pulling local LLM model (qwen2.5-coder:1.5b)..."
ollama pull qwen2.5-coder:1.5b

# 5. Check/Install uv
UV_BIN="$HOME/.local/bin/uv"
if ! command -v uv &> /dev/null && [ ! -f "$UV_BIN" ]; then
    echo "Installing uv (fast Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Set path to include local bin just in case uv was installed
export PATH="$HOME/.local/bin:$PATH"

# 6. Create virtual environment and install dependencies
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

# 7. Create symlink in ~/.local/bin/
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
echo " Launching db-agent..."
echo "=========================================="
