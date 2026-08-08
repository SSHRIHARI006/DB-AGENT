# Taste
- Drives work from repo spec files with terse commands (e.g., "implement @.specs/1_spec.md") and expects end-to-end completion: planning, implementation, tests, and validation checks before the task is finished. Confidence: 0.6
- Prefers to receive copy-paste test snippets to run on their own machine (explicitly asked "Give snippets to test on my machine here") rather than relying only on the agent's test output. Confidence: 0.5
- Uses `uv` for Python project management (virtualenv, lockfile, editable installs, package builds) rather than pip/poetry. Confidence: 0.6
- Prefers explicit, user-confirmed configuration over silent defaults: e.g., per-role model selection where execution is blocked until both Orchestrator and Worker models are explicitly chosen, rather than silently defaulting or switching providers/models. Confidence: 0.5
- Keeps provider API keys in a local `.env` file and expects the tool to read them from there (env-var key references) rather than prompting interactively — explicitly stated for testing purposes. Confidence: 0.8
- Specifies the exact `.env` variable names it wants (conventional lowercase `{provider}_api_key`, e.g. `groq_api_key`, `gemini_api_key`, `ollama-cloud_api_key`) and expects the tool to support that literal format rather than inventing its own prefixed names. Confidence: 0.7
