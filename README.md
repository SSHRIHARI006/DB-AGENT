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

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
python3 install.py
```

The installer does not install or pull local Ollama models. It reuses an existing `.venv` when present, installs the pinned MCP-compatible dependencies, and verifies the FastMCP import before completing.

You can launch with either:

```bash
./db-agent sqlite:///test.db --session my_project
```

or, after installation:

```bash
~/.local/bin/db-agent sqlite:///test.db --session my_project
```

Start a session directly:

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
