# DB-Agent: Autonomous Database Assistant

`db-agent` is a terminal database assistant that plans and executes SQL operations through an Orchestrator and Worker agent. It supports rollback tracking, session isolation, and selectable remote LLM providers.

## Providers

Configure one of these providers inside a session:

- Gemini
- OpenRouter
- Ollama Cloud
- NVIDIA NIM
- Groq

OpenAI and Anthropic are intentionally not included in this provider set.

## Prerequisites

- Python 3.12+
- A SQLAlchemy database URI
- An API key for the selected provider, supplied through the provider-specific environment variable or a masked prompt

Supported `.env` variables:

```text
groq_api_key=
ollama-cloud_api_key=
gemini_api_key=
openrouter_api_key=
nvidia_api_key=
```

The equivalent `DBAGENT_*_KEY` environment variables are also accepted for compatibility.

Keys entered at the prompt are held only in process memory and are never written to session files. Environment configuration stores only the variable name reference.

## Setup

### Linux and macOS

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
python3 install.py
```

The installer does not install or pull local Ollama models. It reuses an existing `.venv` when present, installs the dependency ranges declared in `pyproject.toml`, and verifies the FastMCP import before completing.

You can launch with either:

```bash
./db-agent sqlite:///test.db --session my_project
```

or, after installation:

```bash
~/.local/bin/db-agent sqlite:///test.db --session my_project
```

### Windows setup

Run these commands in PowerShell. Python 3.12 or newer is required.

```powershell
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
py -3.12 install.py
```

If the `py` launcher is unavailable, run `python install.py` instead. After setup, activate the virtual environment in PowerShell with:

```powershell
.\.venv\Scripts\Activate.ps1
```

In Command Prompt, use:

```bat
.venv\Scripts\activate.bat
```

You can also run without activating the environment:

```powershell
.\.venv\Scripts\db-agent.exe sqlite:///test.db --session my_project
```

or:

```powershell
.\.venv\Scripts\python.exe -m db_agent.cli sqlite:///test.db --session my_project
```

For an absolute Windows SQLite path, use forward slashes in the SQLAlchemy URI, for example `sqlite:///C:/Users/you/db-agent/test.db`. The root `db-agent` launcher and `setup.sh` are Unix helpers; use the commands above on Windows.

To run the test suite on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Start a session directly on Unix-like systems with:

```bash
./db-agent sqlite:///test.db --session my_project
```

## Provider and model commands

Inside the session:

```text
/provider set <gemini|openrouter|ollama_cloud|nvidia_nim|groq>
/provider switch <name>
/provider list
/provider status
/models list
/models refresh
/models use orchestrator <model-id>
/models use worker <model-id>
```

A request runs only after both the Orchestrator and Worker models are explicitly selected. Model lists are cached per session for 24 hours, with stale-cache and static fallback behavior for network failures.

## Database commands

```text
/log
/undo
/revert <hash>
/exit
```

Session state is stored under `.db_agent/sessions/<session>/`. Raw provider API keys are never serialized there.

## Database drivers

- SQLite: built into Python
- PostgreSQL: `psycopg2-binary`
- MySQL/MariaDB: `pymysql`
