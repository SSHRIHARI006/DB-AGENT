import json

import httpx
import pytest

from db_agent.model_cache import get_models, models_for_role
from db_agent.provider_config import ProviderConfig, ProviderEntry, selected_models, set_selected_model
from db_agent import cli
from db_agent.providers.base import ModelInfo, ProviderError
from db_agent.providers.groq import GroqAdapter
from db_agent.providers.openrouter import OpenRouterAdapter


class CatalogAdapter:
    async def list_models(self):
        return [
            ModelInfo("chat", "Chat", supports_chat=True, supports_tools=True),
            ModelInfo("unknown-tools", "Unknown Tools", supports_chat=True),
            ModelInfo("orchestrator-only", "Orchestrator", supports_chat=True, supports_tools=False),
            ModelInfo("audio", "Audio", supports_chat=False, modality="audio"),
        ]


@pytest.mark.asyncio
async def test_role_filtering_applies_to_live_and_worker_models(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    orchestrator = await get_models("session", "groq", CatalogAdapter(), role="orchestrator")
    worker = await get_models("session", "groq", CatalogAdapter(), role="worker")
    assert {model.id for model in orchestrator.models} == {"chat", "unknown-tools", "orchestrator-only"}
    assert {model.id for model in worker.models} == {"chat", "unknown-tools"}


def test_role_filtering_rejects_invalid_roles():
    with pytest.raises(ValueError):
        models_for_role([], "invalid")


def test_assigning_one_role_preserves_the_other():
    config = ProviderConfig(
        "groq",
        {"groq": ProviderEntry("env:groq_api_key", worker_model="worker-old")},
    )
    set_selected_model(config, "groq", "orchestrator", "orchestrator-new")
    assert selected_models(config, "groq") == ("orchestrator-new", "worker-old")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [GroqAdapter, OpenRouterAdapter])
async def test_openai_compatible_adapters_filter_non_chat_ids(adapter_type):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "chat-model", "supported_parameters": ["tools"]},
                    {"id": "text-embedding-3-small"},
                    {"id": "whisper-large-v3"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = adapter_type("secret", client=client)
    models = await adapter.list_models()
    assert [model.id for model in models] == ["chat-model"]
    await client.aclose()


@pytest.mark.asyncio
async def test_role_filtering_applies_to_stale_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    await get_models("session", "groq", CatalogAdapter())

    class FailingAdapter:
        async def list_models(self):
            raise ProviderError("groq", "offline")

    import db_agent.model_cache as cache
    from datetime import datetime, timedelta, timezone

    path = tmp_path / ".db_agent" / "sessions" / "session" / "model_cache" / "groq.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(payload))
    result = await get_models("session", "groq", FailingAdapter(), role="worker")
    assert result.source == "stale cache"
    assert [model.id for model in result.models] == ["chat", "unknown-tools"]
    del cache


@pytest.mark.asyncio
async def test_cli_assign_model_saves_only_selected_role(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = ProviderConfig(
        "groq",
        {"groq": ProviderEntry("env:groq_api_key", worker_model="worker-old")},
    )
    monkeypatch.setenv("groq_api_key", "secret")
    monkeypatch.setattr(cli, "load_provider_config", lambda _: config)
    monkeypatch.setattr(cli, "save_provider_config", lambda _, value: None)

    class Adapter:
        async def list_models(self):
            return [ModelInfo("orchestrator-new", "Orchestrator", True, True)]

        async def close(self):
            pass

    monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: Adapter())
    assert await cli._assign_model("session", config, "orchestrator", "orchestrator-new")
    assert selected_models(config, "groq") == ("orchestrator-new", "worker-old")


@pytest.mark.asyncio
async def test_static_fallback_is_role_filtered(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    class FailingAdapter:
        async def list_models(self):
            raise ProviderError("groq", "offline")

    result = await get_models("session", "groq", FailingAdapter(), role="worker")
    assert result.source == "static fallback"
    assert all(model.supports_tools is True for model in result.models)
