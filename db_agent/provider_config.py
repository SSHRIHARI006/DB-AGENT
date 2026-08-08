import getpass
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Any

from db_agent.providers.factory import PROVIDER_ENV_VARS, provider_env_candidates, supported_providers
from db_agent.tracker import get_session_dir


@dataclass
class ProviderEntry:
    api_key_ref: str
    orchestrator_model: str | None = None
    worker_model: str | None = None
    base_url: str | None = None


@dataclass
class ProviderConfig:
    active_provider: str | None = None
    providers: dict[str, ProviderEntry] | None = None

    def __post_init__(self) -> None:
        if self.providers is None:
            self.providers = {}


def _config_path(session_name: str) -> str:
    return os.path.join(get_session_dir(session_name), "provider_config.json")


def load_provider_config(session_name: str) -> ProviderConfig:
    try:
        with open(_config_path(session_name), "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError, TypeError):
        return ProviderConfig()
    providers = {}
    for name, value in (payload.get("providers") or {}).items():
        if not isinstance(value, dict) or not isinstance(value.get("api_key_ref"), str):
            continue
        providers[name] = ProviderEntry(
            api_key_ref=value["api_key_ref"],
            orchestrator_model=value.get("orchestrator_model"),
            worker_model=value.get("worker_model"),
            base_url=value.get("base_url"),
        )
    active = payload.get("active_provider")
    return ProviderConfig(active if active in providers else None, providers)


def save_provider_config(session_name: str, config: ProviderConfig) -> None:
    session_dir = get_session_dir(session_name)
    payload: dict[str, Any] = {
        "active_provider": config.active_provider,
        "providers": {name: asdict(entry) for name, entry in config.providers.items()},
    }
    fd, temporary_path = tempfile.mkstemp(prefix="provider_config.", suffix=".tmp", dir=session_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.replace(temporary_path, _config_path(session_name))
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


_prompt_keys: dict[tuple[str, str], str] = {}


def prompt_key(session_name: str, provider: str) -> str:
    value = getpass.getpass(f"Enter {provider} API key (input hidden): ").strip()
    if not value:
        raise ValueError(f"No API key entered for {provider}.")
    _prompt_keys[(session_name, provider)] = value
    return value


def resolve_api_key(session_name: str, provider: str, entry: ProviderEntry) -> str | None:
    reference = entry.api_key_ref
    if reference == "prompt":
        return _prompt_keys.get((session_name, provider))
    if reference.startswith("env:"):
        return os.environ.get(reference[4:])
    raise ValueError(f"Invalid API key reference for {provider}.")


def key_source(entry: ProviderEntry, session_name: str, provider: str) -> str:
    if entry.api_key_ref == "prompt":
        return "prompt (in memory)" if (session_name, provider) in _prompt_keys else "prompt (re-enter required)"
    if entry.api_key_ref.startswith("env:"):
        return entry.api_key_ref
    return "invalid"


def env_var_for(provider: str) -> str:
    try:
        return PROVIDER_ENV_VARS[provider]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported provider '{provider}'. Choose: {', '.join(supported_providers())}."
        ) from exc


def env_value_for(provider: str) -> tuple[str, str] | None:
    for variable in provider_env_candidates(provider):
        value = os.environ.get(variable)
        if value:
            return variable, value
    return None


def provider_entry_from_key(provider: str, api_key: str | None, *, used_environment: bool) -> ProviderEntry:
    if used_environment:
        return ProviderEntry(api_key_ref=f"env:{env_var_for(provider)}")
    if api_key is None:
        return ProviderEntry(api_key_ref="prompt")
    return ProviderEntry(api_key_ref="prompt")


def set_selected_model(
    config: ProviderConfig,
    provider: str,
    role: str,
    model: str,
) -> None:
    entry = config.providers[provider]
    if role == "orchestrator":
        entry.orchestrator_model = model
    elif role == "worker":
        entry.worker_model = model
    else:
        raise ValueError("Role must be 'orchestrator' or 'worker'.")


def selected_models(config: ProviderConfig, provider: str) -> tuple[str | None, str | None]:
    entry = config.providers[provider]
    return entry.orchestrator_model, entry.worker_model
