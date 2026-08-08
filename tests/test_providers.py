import json

import httpx
import pytest

from db_agent.providers.gemini import GeminiAdapter
from db_agent.providers.groq import GroqAdapter
from db_agent.providers.nvidia_nim import NvidiaNimAdapter
from db_agent.providers.ollama_cloud import OllamaCloudAdapter
from db_agent.providers.openrouter import OpenRouterAdapter
from db_agent.providers.base import GenerationResult, ToolCall


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,base_url",
    [
        (GroqAdapter, "https://api.groq.com/openai/v1"),
        (NvidiaNimAdapter, "https://integrate.api.nvidia.com/v1"),
        (OllamaCloudAdapter, "https://ollama.com/v1"),
    ],
)
async def test_openai_compatible_generation_normalizes_tool_calls(adapter_type, base_url):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "read_query", "arguments": '{"sql_query":"SELECT 1"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = adapter_type("secret", client=client)
    result = await adapter.generate(messages=[], model="model", tools=[{"name": "read_query"}])
    assert result.tool_calls == [ToolCall("read_query", {"sql_query": "SELECT 1"}, "call-1")]
    await client.aclose()


@pytest.mark.asyncio
async def test_gemini_filters_models_and_parses_function_call():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["key"] == "secret"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                        {"name": "models/gemini-test", "displayName": "Test", "supportedGenerationMethods": ["generateContent"]},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"functionCall": {"name": "read_query", "args": {"sql_query": "SELECT 1"}}}]}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GeminiAdapter("secret", client=client)
    assert [m.id for m in await adapter.list_models()] == ["gemini-test"]
    result = await adapter.generate(prompt="read", model="gemini-test", tools=[{"name": "read_query"}])
    assert result.tool_calls[0].name == "read_query"
    await client.aclose()


@pytest.mark.asyncio
async def test_openrouter_validation_uses_one_token_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test:free", "name": "Test"}]})
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter("secret", client=client)
    assert await adapter.validate_key()
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_names_are_requested_scope_only():
    from db_agent.providers import supported_providers

    assert set(supported_providers()) == {"gemini", "openrouter", "ollama_cloud", "nvidia_nim", "groq"}
