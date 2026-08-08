import json
from typing import Any

import httpx

from db_agent.providers.base import (
    GenerationResult,
    ModelInfo,
    ProviderAdapter,
    ProviderAuthError,
    ToolCall,
)
from db_agent.providers.http import request_json


class GeminiAdapter(ProviderAdapter):
    name = "gemini"

    def __init__(self, api_key: str | None, base_url: str | None = None, *, client=None):
        super().__init__(api_key, (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/"))
        self.client = client or httpx.AsyncClient(timeout=45)
        self._owns_client = client is None

    async def list_models(self) -> list[ModelInfo]:
        if not self.api_key:
            raise ProviderAuthError(self.name, "an API key is required")
        models: list[ModelInfo] = []
        page_token = None
        while True:
            params: dict[str, Any] = {"key": self.api_key}
            if page_token:
                params["pageToken"] = page_token
            payload = await request_json(
                self.client,
                self.name,
                "GET",
                f"{self.base_url}/models",
                params=params,
            )
            for value in payload.get("models", []):
                if "generateContent" not in (value.get("supportedGenerationMethods") or []):
                    continue
                model_id = str(value.get("name", "")).removeprefix("models/")
                if not model_id:
                    continue
                models.append(
                    ModelInfo(
                        id=model_id,
                        display_name=str(value.get("displayName") or model_id),
                        context_window=value.get("inputTokenLimit"),
                        supports_tools=True,
                        supports_thinking="thinking" in model_id.lower() or None,
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return models

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
        contents = self._contents(prompt, messages)
        payload: dict[str, Any] = {"contents": contents}
        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if response_format == "json":
            generation_config.update({"responseMimeType": "application/json"})
        if generation_config:
            payload["generationConfig"] = generation_config
        if tools:
            payload["tools"] = [{"functionDeclarations": [self._tool_schema(tool) for tool in tools]}]
        request_model = model.removeprefix("models/")
        result = await request_json(
            self.client,
            self.name,
            "POST",
            f"{self.base_url}/models/{request_model}:generateContent",
            params={"key": self.api_key},
            payload=payload,
        )
        return self._parse_generation(result)

    @staticmethod
    def _contents(prompt: str | None, messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        source = list(messages or [])
        if prompt is not None:
            source.append({"role": "user", "content": prompt})
        contents = []
        for message in source:
            role = "model" if message.get("role") == "assistant" else "user"
            content = message.get("content", "")
            if isinstance(content, list):
                parts = content
            else:
                parts = [{"text": str(content)}]
            contents.append({"role": role, "parts": parts})
        return contents

    @staticmethod
    def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
        if "function" in tool:
            tool = tool["function"]
        return {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {}),
        }

    @staticmethod
    def _parse_generation(payload: dict[str, Any]) -> GenerationResult:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        candidates = payload.get("candidates") or []
        finish_reason = None
        if candidates:
            candidate = candidates[0] or {}
            finish_reason = candidate.get("finishReason")
            for part in (candidate.get("content") or {}).get("parts", []):
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                function_call = part.get("functionCall")
                if isinstance(function_call, dict) and function_call.get("name"):
                    arguments = function_call.get("args") or {}
                    calls.append(ToolCall(function_call["name"], arguments))
        return GenerationResult("\n".join(text_parts), calls, finish_reason)

    async def validate_key(self) -> bool:
        if not self.api_key:
            raise ProviderAuthError(self.name, "an API key is required")
        await self.list_models()
        return True

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
