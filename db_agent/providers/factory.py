from db_agent.providers.base import ProviderAdapter, ProviderRequestError
from db_agent.providers.gemini import GeminiAdapter
from db_agent.providers.groq import GroqAdapter
from db_agent.providers.nvidia_nim import NvidiaNimAdapter
from db_agent.providers.ollama_cloud import OllamaCloudAdapter
from db_agent.providers.openrouter import OpenRouterAdapter


PROVIDER_ENV_VARS = {
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "ollama_cloud": "ollama-cloud_api_key",
    "nvidia_nim": "nvidia_api_key",
    "groq": "groq_api_key",
}

LEGACY_PROVIDER_ENV_VARS = {
    "gemini": "DBAGENT_GEMINI_KEY",
    "openrouter": "DBAGENT_OPENROUTER_KEY",
    "ollama_cloud": "DBAGENT_OLLAMA_CLOUD_KEY",
    "nvidia_nim": "DBAGENT_NVIDIA_NIM_KEY",
    "groq": "DBAGENT_GROQ_KEY",
}


def provider_env_candidates(provider: str) -> tuple[str, ...]:
    return (PROVIDER_ENV_VARS[provider], LEGACY_PROVIDER_ENV_VARS[provider])


PROVIDER_TYPES = {
    "gemini": GeminiAdapter,
    "openrouter": OpenRouterAdapter,
    "ollama_cloud": OllamaCloudAdapter,
    "nvidia_nim": NvidiaNimAdapter,
    "groq": GroqAdapter,
}


def create_adapter(
    provider: str,
    api_key: str | None,
    base_url: str | None = None,
    *,
    client=None,
) -> ProviderAdapter:
    try:
        adapter_type = PROVIDER_TYPES[provider]
    except KeyError as exc:
        raise ProviderRequestError(provider, "unsupported provider") from exc
    return adapter_type(api_key, base_url, client=client)


def supported_providers() -> tuple[str, ...]:
    return tuple(PROVIDER_TYPES)
