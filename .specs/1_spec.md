# Spec 01 — BYO API Key + Provider/Model Selection

**Status:** Draft for implementation
**Priority:** 1 (highest)
**Depends on:** nothing (foundational — #2 and #3 build on this)

---

## 1. Goal

Replace the hardcoded Ollama-local (`qwen2.5-coder:7b` / `0.5b`) inference layer with a provider-agnostic abstraction so a user can bring their own API key for any of:

- OpenAI
- Claude (Anthropic)
- Gemini (Google)
- OpenRouter
- Ollama Cloud (default — free tier, no key required for baseline usage but supports one)

For each selected provider, the system must be able to **list the models actually available to that key** rather than hardcoding a model name, so the user picks from a live (or cached) list.

This spec covers the provider abstraction + model listing only. Assigning specific models to Orchestrator vs Worker roles is Spec 02.

---

## 2. Scope

**In scope:**
- Provider adapter interface + 5 concrete adapters
- Key storage (session-scoped, not global)
- Model listing per provider (live fetch + cached fallback)
- CLI commands for provider/key/model setup
- Validation of key on entry (cheap connectivity check, not a full call)

**Out of scope (later specs):**
- Orchestrator/Worker separate model assignment (Spec 02)
- Streamlit UI for any of this (Spec 04) — CLI only for now, but design the config layer so Streamlit can reuse it without changes
- Guardrails/permissioning (Spec 05)

---

## 3. Provider Abstraction Layer

### 3.1 Interface

Every provider is wrapped behind one interface so the rest of the codebase (Orchestrator, Worker, tracker) never touches a provider SDK directly:

```python
class ProviderAdapter(ABC):
    name: str  # "openai", "anthropic", "gemini", "openrouter", "ollama_cloud"

    def __init__(self, api_key: str | None, base_url: str | None = None): ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Return models available to this key. Raises ProviderAuthError on bad key."""

    @abstractmethod
    async def generate(self, prompt: str, model: str, **kwargs) -> str:
        """Single-shot text generation, used by both Orchestrator and Worker."""

    @abstractmethod
    async def validate_key(self) -> bool:
        """Cheap check — does NOT count as a real generation call."""
```

```python
@dataclass
class ModelInfo:
    id: str            # what gets stored in config and passed to generate()
    display_name: str
    context_window: int | None = None   # not all providers expose this
```

### 3.2 Per-provider model-listing behavior (verified endpoints)

| Provider | Endpoint | Auth | Notes |
|---|---|---|---|
| **OpenAI** | `GET https://api.openai.com/v1/models` | `Authorization: Bearer <key>` | Returns all models visible to the key, including non-chat ones — filter to chat-capable IDs client-side. |
| **Anthropic** | `GET https://api.anthropic.com/v1/models` | `x-api-key: <key>` | Confirmed live endpoint, paginated (`after_id`/`before_id`, default limit 20). Most recent models listed first. |
| **Gemini** | `GET https://generativelanguage.googleapis.com/v1beta/models` | `?key=<key>` query param or header | Returns models + supported generation methods; filter to ones supporting `generateContent`. |
| **OpenRouter** | `GET https://openrouter.ai/api/v1/models` | none required (public catalog) | Full catalog regardless of key — pricing/context info included. Key is only needed at generation time. |
| **Ollama Cloud** | `GET https://ollama.com/v1/models` | `Authorization: Bearer <OLLAMA_API_KEY>` | OpenAI-compatible endpoint. Follow up with native `/api/show` per model to get capability flags (tools/thinking support) — needed per the capability-display decision below. |

**Capability display (resolved):** `/models list` shows more than raw IDs — context window size, tool-calling support, and thinking/reasoning support where the provider exposes it, so the user can pick a model that actually fits Orchestrator vs Worker needs (Spec 02) rather than guessing from a bare name.

### 3.3 Caching strategy

Live-fetch on every CLI startup is wasteful and fragile (network hiccup shouldn't block the user from working). Design:

- On `provider set`, fetch and cache the model list to `.db_agent/sessions/<id>/model_cache/<provider>.json` with a timestamp.
- Cache TTL: 24h. Past that, refresh silently in the background on next use; if refresh fails, keep serving the stale cache and warn once.
- Explicit `/models refresh` command to force it.
- Ship a small **static fallback list** per provider (top 5–6 common model IDs) baked into the codebase, used only if both live fetch and cache are unavailable (e.g. first run, no network). This guarantees the tool never hard-blocks on model selection.

---

## 4. Key Storage & Config

- **Session-scoped, not global** — consistent with the existing `.db_agent/sessions/<session_id>/` isolation model.
- **Multi-provider, not single-provider.** A user can configure keys for several providers in the same session and switch between them freely — configuring OpenRouter doesn't overwrite an existing Anthropic setup. Store in `.db_agent/sessions/<id>/provider_config.json`:
  ```json
  {
    "active_provider": "anthropic",
    "providers": {
      "anthropic": {
        "api_key_ref": "env:DBAGENT_ANTHROPIC_KEY",
        "orchestrator_model": null,
        "worker_model": null
      },
      "openrouter": {
        "api_key_ref": "prompt",
        "orchestrator_model": null,
        "worker_model": null
      }
    }
  }
  ```
- `api_key_ref` is either `"env:<VAR_NAME>"` or the literal string `"prompt"` — the latter means the key is never persisted and must be re-entered (or re-typed once and cached in-memory for the process lifetime) each session.
- **Key entry (resolved):** both methods supported.
  - **Default:** interactive masked prompt (typed input not echoed to terminal, never written to disk). This is what runs when `/provider key` is invoked without an env var already set.
  - **Shortcut:** environment variable per provider (e.g. `DBAGENT_OPENAI_KEY`, `DBAGENT_ANTHROPIC_KEY`, `DBAGENT_GEMINI_KEY`, `DBAGENT_OPENROUTER_KEY`, `DBAGENT_OLLAMA_CLOUD_KEY`). If set, `/provider set <name>` picks it up automatically and skips the prompt.
  - **Never store raw key values in the JSON file** — only the reference (env var name or the `"prompt"` marker).
- `/provider switch <name>` changes `active_provider` without touching any other provider's stored config — this is the mechanism for freely switching between already-configured providers.
- Add `.db_agent/sessions/*/provider_config.json` handling to whatever `.gitignore` / secrets hygiene already exists for the sessions directory — confirm this is already covered, since it currently stores chat memory too.

---

## 5. Validation on Entry

When a user sets a provider + key, run `validate_key()` before accepting it:
- OpenAI/Anthropic/Gemini/Ollama Cloud: a lightweight authenticated request — the `list_models` call itself doubles as validation, no separate cheap endpoint needed for most of these.
- OpenRouter: since listing models needs no auth, validate by making a minimal `generate()` call with `max_tokens: 1` instead, or skip validation until first real use and surface the error there — flag this as an open decision (see below).

On failure: clear error message naming which provider rejected the key, no silent fallback to a different provider.

---

## 6. CLI UX

New commands (exact syntax up to you, suggested shape):

```
/provider set <openai|anthropic|gemini|openrouter|ollama_cloud>   # configure a new provider (prompts for key unless env var found)
/provider switch <name>           # change active provider among already-configured ones, no re-entry needed
/provider list                    # show all configured providers and which is active
/models list                      # shows cached/live list (with capability info) for active provider
/models refresh                   # force re-fetch for active provider
/provider status                  # shows active provider, key source (env/prompt), cache age
```

Design note: keep this a distinct layer from the eventual Orchestrator/Worker model *assignment* commands (Spec 02) — `/provider` and `/models` here are about **what's available**, not what's in use.

---

## 7. Integration Points (existing 3 files)

- **CLI layer** — add the four commands above; render model list via existing Rich table pattern used elsewhere.
- **Core logic (FastMCP / orchestration loop)** — every place that currently calls Ollama directly gets routed through `ProviderAdapter.generate()`. This is the main refactor: locate every hardcoded `qwen2.5-coder` reference and replace with a config-driven adapter instance.
- **Tracker logic** — no changes needed for this spec; model/provider identity could optionally be logged per commit for audit purposes later, but not required for v1.

---

## 8. Acceptance Criteria

- [ ] Can set any of the 5 providers and enter a key without restarting the CLI
- [ ] `/models list` returns real, current model IDs for OpenAI, Anthropic, Gemini, OpenRouter, and Ollama Cloud
- [ ] Invalid key produces a clear, provider-specific error, not a stack trace
- [ ] Model list is cached and survives a session restart without re-hitting the network
- [ ] No raw API key ever written to disk in plaintext
- [ ] Static fallback list works when offline

---

## 9. Decisions (resolved)

1. **OpenRouter validation** — resolved. Validate via a throwaway 1-token generation call (smallest/cheapest model) immediately on key entry, matching the fail-fast behavior of the other 4 providers.
2. **Multi-provider architecture** — resolved. Users can configure multiple providers in one session and switch freely (`/provider switch`); config stores all of them under `providers`, not just one active slot. See Section 4.
3. **Key entry UX** — resolved. Both supported: interactive masked prompt as default, environment variable per provider as a shortcut that skips the prompt.
4. **Capability filtering** — resolved. Show capability info (context window, tool support, thinking support) alongside model IDs in `/models list`, not just bare names.
5. **Fallback static list maintenance** — resolved. Manual updates only, no automation needed for v1.