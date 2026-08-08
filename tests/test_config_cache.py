import json

import pytest

from db_agent.model_cache import get_models
from db_agent.provider_config import (
    ProviderConfig,
    ProviderEntry,
    key_source,
    load_provider_config,
    resolve_api_key,
    save_provider_config,
)
from db_agent.providers.base import ModelInfo, ProviderError


class FakeAdapter:
    async def list_models(self):
        return [ModelInfo("live-model", "Live")]


@pytest.mark.asyncio
async def test_config_does_not_store_raw_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = ProviderConfig("groq", {"groq": ProviderEntry("env:DBAGENT_GROQ_KEY")})
    save_provider_config("session", config)
    path = tmp_path / ".db_agent" / "sessions" / "session" / "provider_config.json"
    assert "secret" not in path.read_text()
    assert load_provider_config("session").providers["groq"].api_key_ref == "env:DBAGENT_GROQ_KEY"


@pytest.mark.asyncio
async def test_cache_uses_live_models(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = await get_models("session", "groq", FakeAdapter())
    assert result.source == "live"
    assert result.models[0].id == "live-model"


@pytest.mark.asyncio
async def test_cache_uses_static_fallback_when_live_listing_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    class FailingAdapter:
        async def list_models(self):
            raise ProviderError("groq", "offline")

    result = await get_models("session", "groq", FailingAdapter())
    assert result.source == "static fallback"
    assert result.models


def test_env_key_resolution(monkeypatch):
    monkeypatch.setenv("DBAGENT_GROQ_KEY", "secret")
    entry = ProviderEntry("env:DBAGENT_GROQ_KEY")
    assert resolve_api_key("session", "groq", entry) == "secret"
    assert key_source(entry, "session", "groq") == "env:DBAGENT_GROQ_KEY"


@pytest.mark.asyncio
async def test_provider_config_keeps_multiple_providers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = ProviderConfig(
        "groq",
        {
            "groq": ProviderEntry("env:DBAGENT_GROQ_KEY"),
            "gemini": ProviderEntry("env:DBAGENT_GEMINI_KEY"),
        },
    )
    save_provider_config("session", config)
    loaded = load_provider_config("session")
    assert set(loaded.providers) == {"groq", "gemini"}
    assert loaded.active_provider == "groq"


@pytest.mark.asyncio
async def test_stale_cache_is_served_on_refresh_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    await get_models("session", "groq", FakeAdapter())

    class FailingAdapter:
        async def list_models(self):
            raise ProviderError("groq", "offline")

    import db_agent.model_cache as model_cache
    from datetime import datetime, timedelta, timezone
    path = tmp_path / ".db_agent" / "sessions" / "session" / "model_cache" / "groq.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(payload))
    result = await get_models("session", "groq", FailingAdapter())
    assert result.source == "stale cache"
    assert result.warning
    assert model_cache.CACHE_TTL.days == 1
    
    del model_cache


@pytest.mark.asyncio
async def test_prompt_key_not_persisted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("db_agent.provider_config.getpass.getpass", lambda _: "secret")
    from db_agent.provider_config import prompt_key, _prompt_keys
    _prompt_keys.clear()
    assert prompt_key("session", "groq") == "secret"
    config = ProviderConfig("groq", {"groq": ProviderEntry("prompt")})
    save_provider_config("session", config)
    assert "secret" not in (tmp_path / ".db_agent" / "sessions" / "session" / "provider_config.json").read_text()
    _prompt_keys.clear()
    
    assert resolve_api_key("session", "groq", ProviderEntry("prompt")) is None
