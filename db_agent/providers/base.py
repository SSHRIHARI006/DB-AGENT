from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    supports_chat: bool | None = None
    modality: str | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None


class ProviderError(Exception):
    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"{provider}: {message}")


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderRequestError(ProviderError):
    pass


class LLMConnectionError(ProviderError):
    """The model endpoint itself failed (bad model id, no access, network).
    Raised so callers can fail over to another provider/endpoint."""


class ProviderAdapter(ABC):
    name: str

    def __init__(self, api_key: str | None, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    @abstractmethod
    async def generate(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        response_format: str | None = None,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        raise NotImplementedError

    @abstractmethod
    async def validate_key(self) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        return None
