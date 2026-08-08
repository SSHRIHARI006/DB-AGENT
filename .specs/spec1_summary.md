# Spec 01 Summary — BYO API Keys and Provider/Model Selection

## Status

Implemented in the repository. The final implementation follows the later provider-scope decision rather than the original draft’s OpenAI/Anthropic list.

## Objective

Replace the hardcoded local Ollama/Qwen inference path with a provider-agnostic, session-aware provider layer that can:

- Configure multiple providers without restarting the CLI.
- Load provider API keys from the local `.env` file or an interactive masked prompt.
- List models available from a provider.
- Display model capabilities when the provider exposes them.
- Cache model lists for 24 hours and provide stale/static fallbacks.
- Route Orchestrator and Worker generation through one normalized adapter interface.
- Avoid writing raw API keys to session configuration.
- Install cleanly from a fresh GitHub clone without installing local Ollama.

## Final provider scope

The requested final provider set is:

1. `gemini`
2. `openrouter`
3. `ollama_cloud`
4. `nvidia_nim`
5. `groq`

OpenAI and Anthropic were removed from the active provider registry at the later implementation request. They are not exposed by `/provider set`, do not have environment mappings, and are not included in the supported-provider tests.

## API key configuration

The CLI loads `.env` automatically at startup through `db_agent/env_loader.py`.

The exact supported variable names are:

```text
groq_api_key=
ollama-cloud_api_key=
gemini_api_key=
openrouter_api_key=
nvidia_api_key=
```

Legacy compatibility names are also accepted:

```text
DBAGENT_GROQ_KEY
DBAGENT_OLLAMA_CLOUD_KEY
DBAGENT_GEMINI_KEY
DBAGENT_OPENROUTER_KEY
DBAGENT_NVIDIA_NIM_KEY
```

Behavior:

- Existing process environment variables take precedence over `.env` values.
- `.env` values are loaded without requiring `python-dotenv`.
- Blank lines and comments are ignored.
- Matching single or double quotes around values are removed.
- Environment-based provider configuration persists only the variable name, for example `env:groq_api_key`.
- Prompt-entered keys are stored only in process memory under the `prompt` reference.
- Raw key values are never written to `provider_config.json`.
- `.env` and `.env.*` are ignored by Git.
- `.env.example` is included as a safe template with empty values.

## Provider abstraction

The provider interface is defined in `db_agent/providers/base.py`.

### `ProviderAdapter`

Every provider implements:

```python
async def list_models() -> list[ModelInfo]
async def generate(...) -> GenerationResult
async def validate_key() -> bool
async def close() -> None
```

The generation contract was expanded from the original text-only draft because the Worker requires tool calls.

### Normalized data types

`ModelInfo` contains:

- `id`
- `display_name`
- `context_window`
- `supports_tools`
- `supports_thinking`

`ToolCall` contains:

- `name`
- `arguments`
- optional call ID

`GenerationResult` contains:

- generated `text`
- normalized `tool_calls`
- optional finish reason

The Orchestrator consumes `GenerationResult.text`. The Worker consumes normalized tool calls and retains a JSON-text fallback parser.

## Adapter implementation

### Gemini

File: `db_agent/providers/gemini.py`

- Uses the Google Generative Language REST API.
- Lists `/v1beta/models` with the API key query parameter.
- Filters models to those supporting `generateContent`.
- Supports pagination through `nextPageToken`.
- Uses native `generateContent` requests.
- Converts messages, tools, text parts, and function-call parts.
- Uses model listing as key validation.

### OpenRouter

File: `db_agent/providers/openrouter.py`

- Uses the public `/api/v1/models` catalog.
- Uses OpenAI-compatible chat completions for generation.
- Requires an API key for validation.
- Performs a throwaway one-token generation request immediately on provider setup.
- Prefers a `:free` model as the validation candidate when available.

### Ollama Cloud

File: `db_agent/providers/ollama_cloud.py`

- Uses `https://ollama.com/v1`.
- Uses OpenAI-compatible model and chat-completion endpoints.
- Does not reference `localhost:11434`.
- Does not install or pull local Ollama models.
- Uses model listing for validation.

### NVIDIA NIM

File: `db_agent/providers/nvidia_nim.py`

- Uses the OpenAI-compatible endpoint at `https://integrate.api.nvidia.com/v1`.
- Supports model listing, generation, tool calls, and bearer authentication through the shared adapter.

### Groq

File: `db_agent/providers/groq.py`

- Uses the OpenAI-compatible endpoint at `https://api.groq.com/openai/v1`.
- Supports model listing, generation, tool calls, and bearer authentication through the shared adapter.

### Shared HTTP layer

File: `db_agent/providers/http.py`

- Uses `httpx.AsyncClient`.
- Normalizes network and HTTP failures into provider exceptions.
- Maps 401/403 to `ProviderAuthError`.
- Maps 429 to `ProviderRateLimitError`.
- Handles transient 429/5xx responses with one retry.
- Sanitizes provider error messages and does not expose API key headers.

The OpenAI-compatible implementation is shared internally by OpenRouter, Ollama Cloud, NVIDIA NIM, and Groq. This is an internal protocol implementation, not an OpenAI provider registration.

## Provider registry

File: `db_agent/providers/factory.py`

The registry exposes only:

```python
{
    "gemini": GeminiAdapter,
    "openrouter": OpenRouterAdapter,
    "ollama_cloud": OllamaCloudAdapter,
    "nvidia_nim": NvidiaNimAdapter,
    "groq": GroqAdapter,
}
```

The factory also maps provider names to their exact `.env` variables and legacy compatibility variables.

## Session configuration

File: `db_agent/provider_config.py`

Provider state is stored per session at:

```text
.db_agent/sessions/<session>/provider_config.json
```

The structure is:

```json
{
  "active_provider": "groq",
  "providers": {
    "groq": {
      "api_key_ref": "env:groq_api_key",
      "orchestrator_model": "llama-3.3-70b-versatile",
      "worker_model": "llama-3.1-8b-instant",
      "base_url": null
    }
  }
}
```

The config supports:

- Multiple configured providers.
- Active-provider switching.
- Independent Orchestrator and Worker model fields.
- Optional provider base URLs.
- Atomic writes through a temporary file and `os.replace`.
- Prompt keys held only during the current process.

The broad `.db_agent/` Git ignore rule covers provider configuration, model caches, chat memory, rollback state, and session database configuration.

## Model cache

File: `db_agent/model_cache.py`

Cache location:

```text
.db_agent/sessions/<session>/model_cache/<provider>.json
```

Behavior:

- Live model results are cached with an ISO timestamp.
- Cache TTL is 24 hours.
- Fresh cache is served without a network call.
- Expired cache triggers a live refresh.
- If refresh fails, stale cache is served with a warning.
- If no cache exists, a static provider fallback list is used.
- `/models refresh` forces a live refresh.
- Capability fields are retained in the cache.

Static fallback lists are maintained in `db_agent/providers/static_models.py` for all five providers.

## Agent integration

### Orchestrator

File: `db_agent/agents/orchestrator.py`

`plan_dag()` now receives:

- User query.
- Database schema text.
- A configured `ProviderAdapter`.
- The selected Orchestrator model ID.

It requests JSON output and parses the normalized text result. The existing safe single-task fallback remains for provider or parsing failures.

### Worker

File: `db_agent/agents/worker.py`

`execute_task()` now receives:

- Task data.
- Schema text.
- Commit group ID.
- MCP session.
- A configured `ProviderAdapter`.
- The selected Worker model ID.

The Worker:

- Converts MCP tools into provider-neutral function definitions.
- Passes them to the adapter.
- Prefers native normalized tool calls.
- Validates tool names against the MCP tool list.
- Validates that arguments are dictionaries.
- Falls back to parsing JSON tool calls from text.
- Executes only the first valid tool call, preserving existing behavior.
- Adds `commit_group_id` only to mutation tool calls.
- Keeps bounded retry/auto-healing behavior.

No direct Ollama imports or model calls remain in the agents.

## CLI commands

Provider commands are intercepted before natural-language planning:

```text
/provider set <gemini|openrouter|ollama_cloud|nvidia_nim|groq>
/provider switch <name>
/provider list
/provider status
```

Model commands are:

```text
/models list
/models refresh
/models use orchestrator <model-id>
/models use worker <model-id>
```

Normal execution is intentionally blocked until both role models are selected. There is no silent provider or model fallback.

`/models list` displays:

- Model ID.
- Display name.
- Context window.
- Tool support.
- Thinking support.
- Live/cache/fallback source.

Existing database commands remain:

```text
/log
/undo
/revert <hash>
/exit
```

## Installation and clone behavior

The repository is now clone-safe:

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
python3 install.py
./db-agent sqlite:///test.db --session my_project
```

### `install.py`

- Requires Python 3.12+.
- Reuses an existing `.venv` when its Python executable exists.
- Does not recreate an active virtual environment.
- Installs the project with the environment’s Python interpreter.
- Verifies `from mcp.server.fastmcp import FastMCP` before completing.
- Creates or replaces the `~/.local/bin/db-agent` symlink on Unix-like systems.
- Does not install Ollama.
- Does not pull Qwen or any local model.

### `setup.sh`

- Performs the same Python version check.
- Reuses an existing `.venv`.
- Installs the editable package.
- Runs the FastMCP compatibility check.
- Does not install or start Ollama.

### Root launcher

File: `db-agent`

- Resolves its own repository directory.
- Runs `setup.sh` only when `.venv/bin/python` is missing.
- Executes `.venv/bin/python -m db_agent.cli` directly.
- Does not depend on an activated shell PATH or a globally installed console script.

## Dependency compatibility fix

The original dependency declaration used:

```toml
mcp>=0.1.0
```

That allowed pip/uv to install MCP 2.x, which removed the `mcp.server.fastmcp` module used by this code and caused:

```text
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

The final declaration is:

```toml
mcp>=1.0.0,<2.0.0
```

The lockfile is regenerated and resolves a compatible MCP 1.x release. The installer also performs an explicit import check so an incompatible environment cannot be reported as successfully installed.

## Database testing

The CLI accepts SQLAlchemy URLs, not raw filesystem paths.

Correct relative SQLite URL:

```text
sqlite:///test.db
```

Correct absolute Linux SQLite URL:

```text
sqlite:////home/<user>/db-agent/test.db
```

A raw path such as:

```text
/home/<user>/db-agent/test.db
```

is not a valid SQLAlchemy URL.

If `test.db` already exists, it can be used directly with:

```bash
./db-agent sqlite:///test.db --session Test1
```

## Tests and validation

Repository tests are under `tests/` and use mocked HTTP responses rather than real provider calls.

Coverage includes:

- Provider registry scope.
- OpenAI-compatible tool-call normalization.
- Gemini model filtering and function-call parsing.
- OpenRouter one-token validation.
- Session config persistence.
- Multiple provider preservation.
- Raw key exclusion from persisted JSON.
- Prompt-key memory behavior.
- Exact `.env` variable loading.
- Existing environment precedence.
- Live model cache behavior.
- Stale-cache fallback.
- Static model fallback.

Validated commands:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q db_agent tests install.py
uv lock --check
uv build
git diff --check
./db-agent --help
```

Final validation result at implementation time:

```text
15 passed
MCP import OK
clean install OK
package build successful
lockfile valid
no diff whitespace errors
```

A clean scratch virtual environment was also created with `uv`, the editable package was installed, MCP 1.x was resolved, and the FastMCP import succeeded.

## Files added or materially changed

### Added

- `db_agent/providers/base.py`
- `db_agent/providers/http.py`
- `db_agent/providers/openai_compatible.py`
- `db_agent/providers/gemini.py`
- `db_agent/providers/openrouter.py`
- `db_agent/providers/ollama_cloud.py`
- `db_agent/providers/nvidia_nim.py`
- `db_agent/providers/groq.py`
- `db_agent/providers/factory.py`
- `db_agent/providers/static_models.py`
- `db_agent/providers/__init__.py`
- `db_agent/provider_config.py`
- `db_agent/model_cache.py`
- `db_agent/env_loader.py`
- `.env.example`
- `tests/test_providers.py`
- `tests/test_config_cache.py`
- `tests/test_env_loader.py`

### Changed

- `db_agent/agents/orchestrator.py`
- `db_agent/agents/worker.py`
- `db_agent/cli.py`
- `pyproject.toml`
- `uv.lock`
- `install.py`
- `setup.sh`
- `db-agent`
- `README.md`
- `.gitignore`

## Known limitations and follow-up work

- The original draft’s OpenAI and Anthropic adapters are intentionally not implemented because the final provider decision replaced them with NVIDIA NIM and Groq.
- Live provider validation requires real network access and valid provider credentials; repository tests use mocks only.
- Provider capability metadata is incomplete when an API does not expose tool or thinking support. Unknown values are shown as `unknown` rather than guessed.
- Ollama Cloud capability enrichment through native model metadata is not implemented; the provider uses the OpenAI-compatible cloud endpoints.
- Model assignment was originally described as Spec 02, but explicit Orchestrator/Worker selection was added because the final testing requirement requested both agents be chosen before execution.
- The Worker still executes only the first valid tool call per task.
- Session database URIs are still stored by the existing session configuration system and may contain database credentials if the URI includes them. Provider API keys are handled separately and are never written as raw values.
- The repository changes must be committed and pushed to GitHub before a new remote clone can receive them.

## Recommended first-run test

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
cp .env.example .env
# Fill one or more exact provider variables in .env
python3 install.py
./db-agent sqlite:///test.db --session Test1
```

Inside the CLI:

```text
/provider set groq
/models list
/models use orchestrator <model-id-from-list>
/models use worker <model-id-from-list>
/provider status
List all users
```

Do not commit `.env`; it is intentionally ignored by Git.
