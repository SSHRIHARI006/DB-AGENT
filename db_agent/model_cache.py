import datetime as dt
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from db_agent.providers import ModelInfo, ProviderAdapter, ProviderError
from db_agent.providers.static_models import STATIC_MODELS
from db_agent.tracker import get_session_dir

CACHE_TTL = dt.timedelta(hours=24)
VALID_ROLES = {"orchestrator", "worker"}


def models_for_role(models: list[ModelInfo], role: str | None) -> list[ModelInfo]:
    if role is None:
        return list(models)
    if role not in VALID_ROLES:
        raise ValueError("Role must be 'orchestrator' or 'worker'.")

    filtered = []
    for model in models:
        if model.supports_chat is False:
            continue
        if model.modality and model.modality.lower() in {
            "audio",
            "embedding",
            "image",
            "vision-only",
            "image-only",
            "moderation",
        }:
            continue
        if role == "worker" and model.supports_tools is False:
            continue
        filtered.append(model)
    return filtered


@dataclass
class ModelListResult:
    models: list[ModelInfo]
    source: str
    fetched_at: dt.datetime | None = None
    warning: str | None = None


def _cache_path(session_name: str, provider: str) -> str:
    directory = os.path.join(get_session_dir(session_name), "model_cache")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{provider}.json")


def _read_cache(session_name: str, provider: str) -> tuple[list[ModelInfo], dt.datetime] | None:
    try:
        with open(_cache_path(session_name, provider), "r", encoding="utf-8") as file:
            payload = json.load(file)
        fetched_at = dt.datetime.fromisoformat(payload["fetched_at"])
        models = [ModelInfo(**value) for value in payload.get("models", [])]
        return models, fetched_at
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(session_name: str, provider: str, models: list[ModelInfo]) -> dt.datetime:
    fetched_at = dt.datetime.now(dt.timezone.utc)
    payload: dict[str, Any] = {
        "fetched_at": fetched_at.isoformat(),
        "models": [asdict(model) for model in models],
    }
    with open(_cache_path(session_name, provider), "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
    return fetched_at


async def get_models(
    session_name: str,
    provider: str,
    adapter: ProviderAdapter,
    *,
    role: str | None = None,
    force_refresh: bool = False,
) -> ModelListResult:
    cached = _read_cache(session_name, provider)
    now = dt.datetime.now(dt.timezone.utc)
    if cached and not force_refresh:
        models, fetched_at = cached
        if now - fetched_at < CACHE_TTL:
            return ModelListResult(models_for_role(models, role), "cache", fetched_at)

    try:
        models = await adapter.list_models()
        fetched_at = _write_cache(session_name, provider, models)
        return ModelListResult(models_for_role(models, role), "live", fetched_at)
    except ProviderError as exc:
        if cached:
            models, fetched_at = cached
            return ModelListResult(
                models_for_role(models, role),
                "stale cache",
                fetched_at,
                f"Live model refresh failed: {exc}",
            )
        models = STATIC_MODELS.get(provider, [])
        return ModelListResult(
            models_for_role(list(models), role),
            "static fallback",
            None,
            f"Live model listing failed: {exc}",
        )
