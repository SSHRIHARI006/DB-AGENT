"""Demo-mode configuration: pinned cheap models and round-robin API key rotation.

Everything in this module is demo-mode-only. Local/CLI mode keeps its
interactive provider setup and live model catalog; the demo fixes both models
at deploy time and spreads traffic across a comma-separated list of keys
round-robin, per query, with an in-memory counter (no cross-restart state).
"""

from __future__ import annotations

import itertools
import os

# Fixed at deploy time — deliberately not resolved from /models list. The
# worker must be tool-capable; the orchestrator just needs to be fast/cheap.
DEMO_ORCHESTRATOR_MODEL = "llama-3.1-8b-instant"
DEMO_WORKER_MODEL = "llama-3.1-8b-instant"


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
