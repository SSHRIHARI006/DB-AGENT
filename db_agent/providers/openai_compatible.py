from typing import Any

import httpx

from db_agent.providers.base import (
    GenerationResult,
    ModelInfo,
    ProviderAdapter,
    ProviderAuthError,
    ToolCall,
)
from db_agent.providers.http import auth_headers, request_json


class OpenAICompatibleAdapter(ProviderAdapter):
    models_path = "/models"
    chat_path = "/chat/completions"
    include_all_models = False

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(api_key, base_url.rstrip("/"))
        self.client = client or httpx.AsyncClient(timeout=45)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        return auth_headers(self.api_key)

    async def list_models(self) -> list[ModelInfo]:
        payload = await request_json(
            self.client,
            self.name,
            "GET",
            f"{self.base_url}{self.models_path}",
            headers=self._headers(),
        )
        values = payload.get("data", payload.get("models", []))
        if not isinstance(values, list):
            return []
        models = []
        for value in values:
            if isinstance(value, str):
                model_id = value
                metadata = {}
            else:
                model_id = value.get("id") or value.get("name")
                metadata = value
            if not model_id:
                continue
            if not self.include_all_models and self._is_non_generation_model(str(model_id)):
                continue
            models.append(self._model_info(str(model_id), metadata))
        return models

    def _model_info(self, model_id: str, metadata: dict[str, Any]) -> ModelInfo:
        context = metadata.get("context_length") or metadata.get("context_window")
        architecture = metadata.get("architecture") or {}
        supported = metadata.get("supported_parameters") or []
        if isinstance(supported, dict):
            supported = supported.keys()
        supports_tools = None
        if supported:
            supports_tools = "tools" in supported or "tool_choice" in supported
        if supports_tools is None and isinstance(architecture, dict):
            supports_tools = architecture.get("supports_tools")
        supports_thinking = metadata.get("supports_thinking")
        return ModelInfo(
            id=model_id,
            display_name=str(metadata.get("name") or metadata.get("display_name") or model_id),
            context_window=int(context) if isinstance(context, (int, float)) else None,
            supports_tools=supports_tools,
            supports_thinking=supports_thinking,
        )

    @staticmethod
    def _is_non_generation_model(model_id: str) -> bool:
        lowered = model_id.lower()
        return any(
            marker in lowered
            for marker in ("embedding", "moderation", "whisper", "tts", "dall-e")
        )

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
        request_messages = list(messages or [])
        if prompt is not None:
            request_messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": model, "messages": request_messages}
        if tools:
            payload["tools"] = [self._tool_schema(tool) for tool in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        result = await request_json(
            self.client,
            self.name,
            "POST",
            f"{self.base_url}{self.chat_path}",
            headers=self._headers(),
            payload=payload,
        )
        return self._parse_generation(result)

    @staticmethod
    def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
        if "function" in tool:
            return tool
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
        }

    @staticmethod
    def _parse_generation(payload: dict[str, Any]) -> GenerationResult:
        choices = payload.get("choices") or []
        if not choices:
            return GenerationResult()
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                import json
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if isinstance(arguments, dict) and function.get("name"):
                calls.append(
                    ToolCall(
                        name=function["name"],
                        arguments=arguments,
                        id=call.get("id"),
                    )
                )
        return GenerationResult(
            text=content if isinstance(content, str) else str(content),
            tool_calls=calls,
            finish_reason=choice.get("finish_reason"),
        )

    async def validate_key(self) -> bool:
        if not self.api_key:
            raise ProviderAuthError(self.name, "an API key is required")
        await self.list_models()
        return True

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
