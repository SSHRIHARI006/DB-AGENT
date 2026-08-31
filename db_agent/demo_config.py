"""Demo-mode configuration: rotating provider+model endpoints and API keys.

Everything in this module is demo-mode-only. Local/CLI mode keeps its
interactive provider setup and live model catalog. The demo builds a small
endpoint pool at deploy time — 1-2 tool-capable models per provider that has a
configured key — and spreads traffic across it round-robin, per query, so no
single provider's key/quota gets exhausted. Keys within a provider rotate
round-robin too, per query, with an in-memory counter (no cross-restart state).
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass

from db_agent.providers.static_models import STATIC_MODELS

# How many models per provider to include in the demo rotation pool. The
# orchestrator just needs to be fast/cheap; the worker must be tool-capable.
# Keeping this at 1-2 spreads load across providers instead of hammering one.
MODELS_PER_PROVIDER = 2

# Provider order for the pool: rotate in this order so traffic spreads across
# all configured providers evenly.
POOL_PROVIDERS = ("groq", "gemini", "openrouter", "ollama_cloud", "nvidia_nim")


@dataclass(frozen=True)
class DemoEndpoint:
    """A rotating endpoint: provider, api key, and orchestrator/worker models."""

    provider: str
    api_key: str
    orchestrator_model: str
    worker_model: str


def demo_env_var(provider: str) -> str:
    """Env var that holds the comma-separated demo key list for a provider."""
    return f"DEMO_{provider.upper()}_KEYS"


def demo_api_keys(provider: str) -> list[str]:
    """Return the demo key list for a provider.

    Prefers ``DEMO_<PROVIDER>_KEYS`` (comma-separated); falls back to the
    standard single-key env var so a non-demo deployment keeps working with
    no extra configuration.
    """
    raw = os.environ.get(demo_env_var(provider), "")
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if keys:
        return keys
    from db_agent.provider_config import env_value_for

    env_cfg = env_value_for(provider)
    if env_cfg:
        return [env_cfg[1]]
    return []


def _new_rotator(keys: list[str]):
    """Create a round-robin key iterator for a fixed key list."""
    return itertools.cycle(keys)


def make_key_rotator(provider: str):
    """Create a round-robin key rotator for a provider's demo key list."""
    return _new_rotator(demo_api_keys(provider))


def build_endpoint_pool() -> list[DemoEndpoint]:
    """Build the demo endpoint pool from configured providers.

    A provider is included only if it has at least one resolvable key. For
    each such provider we take up to ``MODELS_PER_PROVIDER`` tool-capable
    models from the static catalog, preferring the first entries (which are
    the cheapest/fastest). The orchestrator uses the first model; the worker
    uses the first model that supports tools (falling back to the same one).
    """
    pool: list[DemoEndpoint] = []
    for provider in POOL_PROVIDERS:
        keys = demo_api_keys(provider)
        if not keys:
            continue
        models = STATIC_MODELS.get(provider, [])
        candidates = [
            m for m in models
            if m.supports_chat is not False
            and m.modality in (None, "", "text")
        ][:MODELS_PER_PROVIDER]
        if not candidates:
            continue
        for model in candidates:
            worker_model = next(
                (m.id for m in candidates if m.supports_tools is not False),
                model.id,
            )
            pool.append(
                DemoEndpoint(
                    provider=provider,
                    api_key=keys[0],
                    orchestrator_model=model.id,
                    worker_model=worker_model,
                )
            )
    return pool


_pool: list[DemoEndpoint] | None = None
_pool_iter: itertools.cycle | None = None


def _ensure_pool() -> None:
    """Build the endpoint pool once and keep a round-robin iterator over it."""
    global _pool, _pool_iter
    if _pool is None:
        _pool = build_endpoint_pool()
        _pool_iter = itertools.cycle(_pool) if _pool else None


def next_demo_endpoint(skip: set[str] | None = None) -> DemoEndpoint | None:
    """Return the next pool endpoint round-robin, skipping ``skip`` providers.

    ``skip`` is a set of provider names (e.g. ones that failed this session)
    so a dead endpoint fails over to the next provider instead of being
    repeated. Returns None if no pool exists or every endpoint is skipped.
    """
    _ensure_pool()
    if _pool_iter is None:
        return None
    for _ in range(len(_pool) * 2):
        endpoint = next(_pool_iter)
        if skip is None or endpoint.provider not in skip:
            return endpoint
    return None
