import json
from typing import Any

import httpx

from db_agent.providers.base import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
)


async def request_json(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts = 2
    for attempt in range(attempts):
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError(provider, "network request failed") from exc

        if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
            continue

        if response.status_code in {401, 403}:
            raise ProviderAuthError(provider, "the API key was rejected")
        if response.status_code == 429:
            raise ProviderRateLimitError(provider, "the provider rate limit was reached")
        if response.status_code >= 400:
            raise ProviderRequestError(provider, _error_detail(response))

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderRequestError(provider, "provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderRequestError(provider, "provider returned an unexpected response")
        return data

    raise ProviderRequestError(provider, "provider request failed after retry")


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if isinstance(error, dict):
                message = error.get("message") or error.get("detail")
                if message:
                    return str(message)[:300]
            if isinstance(error, str):
                return error[:300]
    except ValueError:
        pass
    return f"HTTP {response.status_code}"


def auth_headers(api_key: str | None, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra:
        headers.update(extra)
    return headers
