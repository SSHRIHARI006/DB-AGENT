import os
import sys
import argparse
import asyncio
import json
import re
import secrets
import tty
import termios
import shutil
import datetime
from typing import List, Dict, Any

from sqlalchemy import create_engine, text
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

from db_agent.env_loader import load_dotenv
from db_agent.tracker import (
    init_session,
    load_chat_memory,
    save_chat_memory,
    load_rollback_stack,
    load_query_history,
    save_rollback_stack,
    generate_inverse_queries,
    save_session_config,
    load_session_config,
    get_all_sessions,
    get_session_dir,
)
from db_agent.model_cache import get_models, models_for_role
from db_agent.provider_config import (
    ProviderConfig,
    ProviderEntry,
    env_value_for,
    env_var_for,
    key_source,
    load_provider_config,
    prompt_key,
    resolve_api_key,
    save_provider_config,
    selected_models,
    set_selected_model,
)
from db_agent.providers import (
    ProviderError,
    create_adapter,
    supported_providers,
)
from db_agent.providers.factory import PROVIDER_ENV_VARS
from db_agent.providers.static_models import STATIC_MODELS

ROLE_LABELS = {"orchestrator": "Orchestrator", "worker": "Worker"}

console = Console()

def print_banner(db_uri: str, session_name: str):
    config = load_provider_config(session_name)
    provider = config.active_provider or "not configured"
    orchestrator_model = "not selected"
    worker_model = "not selected"
    if provider in config.providers:
        orchestrator_model, worker_model = selected_models(config, provider)
    banner_text = (
        f"# db-agent CLI\n\n"
        f"**Database URI:** `{db_uri}`\n"
        f"**Active Session:** `{session_name}`\n"
        f"**Provider:** `{provider}`\n"
        f"**Orchestrator:** `{orchestrator_model or 'not selected'}`\n"
        f"**Worker:** `{worker_model or 'not selected'}`\n\n"
        f"Ready to accept database requests in natural language.\n"
        f"Type `/` at an empty prompt to open the command menu.\n\n"
        f"**Setup and Provider Commands:**\n"
        f"- `/providers` : Open the interactive setup menu.\n"
        f"- `/provider set <name>` : Configure a provider.\n"
        f"- `/provider switch <name>` : Switch active provider.\n"
        f"- `/provider assign <role> <model>` : Assign a role model.\n"
        f"- `/provider list` / `/provider status` : Inspect provider state.\n\n"
        f"**Model Commands:**\n"
        f"- `/models list [--role worker|orchestrator]` : List available models.\n"
        f"- `/models refresh` : Refresh the model catalog.\n"
        f"- `/models use <role> <model>` : Assign a role model.\n\n"
        f"**Database Commands:**\n"
        f"- `/log` : Display commit history.\n"
        f"- `/undo` : Revert the latest mutation group.\n"
        f"- `/revert <hash>` : Restore a commit checkpoint.\n"
        f"- `/exit` or `exit` : Leave the session."
    )
    console.print(Panel(Markdown(banner_text), border_style="cyan", expand=False))

def get_user_input() -> str:
    try:
        sys.stdout.write("\ndb-agent> ")
        sys.stdout.flush()
        chars = []
        while True:
            char = get_char()
            if not char:
                return "/exit"
            if char == "/" and not chars:
                sys.stdout.write("/\n")
                sys.stdout.flush()
                return _slash_command_menu()
            if char in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars)
            if char == "\x03":
                return "/exit"
            if char in ("\x08", "\x7f"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char.startswith("\x1b"):
                continue
            chars.append(char)
            sys.stdout.write(char)
            sys.stdout.flush()
    except (KeyboardInterrupt, EOFError):
        return "/exit"


def _slash_command_menu() -> str:
    commands = [
        ("/providers  — setup menu", "/providers"),
        ("/provider set  — configure provider", "provider_set"),
        ("/provider switch  — switch provider", "provider_switch"),
        ("/provider assign  — assign role model", "provider_assign"),
        ("/provider list", "/provider list"),
        ("/provider status", "/provider status"),
        ("/models list", "/models list"),
        ("/models list --role  — role filter", "models_role"),
        ("/models refresh", "/models refresh"),
        ("/models use  — assign role model", "models_use"),
        ("/undo", "/undo"),
        ("/log", "/log"),
        ("/revert  — restore commit", "revert"),
        ("/exit", "/exit"),
    ]
    selected = _select_menu("Select a command", commands)
    if selected is None:
        return ""
    if selected == "provider_set":
        provider = _select_menu(
            "Select a provider",
            [(name, name) for name in supported_providers()],
        )
        return f"/provider set {provider}" if provider else ""
    if selected == "provider_switch":
        session_name = open(os.path.join(".db_agent", "active_session.txt"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(".db_agent", "active_session.txt")) else "default"
        config = load_provider_config(session_name)
        provider = _select_menu(
            "Switch provider",
            [(name, name) for name in config.providers],
        )
        return f"/provider switch {provider}" if provider else ""
    if selected in {"provider_assign", "models_use"}:
        role = _select_menu("Select a role", [(name.title(), name) for name in ROLE_LABELS])
        if not role:
            return ""
        model = input("Model ID: ").strip()
        command = "/provider assign" if selected == "provider_assign" else "/models use"
        return f"{command} {role} {model}" if model else ""
    if selected == "models_role":
        role = _select_menu("Select a role", [(name.title(), name) for name in ROLE_LABELS])
        return f"/models list --role {role}" if role else ""
    if selected == "revert":
        commit_hash = input("Commit hash: ").strip()
        return f"/revert {commit_hash}" if commit_hash else ""
    return selected

async def check_dependencies(db_uri: str) -> bool:
    try:
        engine = create_engine(db_uri)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        console.print("[red]Database Connection Error: Cannot connect using the provided URI.[/red]")
        console.print(f"[red]Detail: {exc}[/red]")
        return False
    return True


def _provider_config_or_error(session_name: str) -> ProviderConfig | None:
    config = load_provider_config(session_name)
    if not config.active_provider or config.active_provider not in config.providers:
        console.print("[yellow]No active provider. Use /provider set <name> first.[/yellow]")
        return None
    return config


def _model_label(model) -> str:
    tools = "✓" if model.supports_tools is True else "✗" if model.supports_tools is False else "?"
    context = "—" if not model.context_window else (
        f"{model.context_window // 1_000_000}M" if model.context_window >= 1_000_000
        else f"{model.context_window // 1_000}K"
    )
    return f"{model.id}  tools {tools}  ctx {context}"


def _role_model_is_valid(model, role: str) -> bool:
    return model in models_for_role([model], role)


async def _configuration_is_complete(session_name: str, config: ProviderConfig) -> bool:
    if not config.active_provider or config.active_provider not in config.providers:
        return False
    provider = config.active_provider
    entry = config.providers[provider]
    adapter = None
    try:
        api_key = resolve_api_key(session_name, provider, entry)
        if not api_key:
            return False
        orchestrator_model, worker_model = selected_models(config, provider)
        if not orchestrator_model or not worker_model:
            return False
        adapter = create_adapter(provider, api_key, entry.base_url)
        result = await get_models(session_name, provider, adapter)
        models = {model.id: model for model in result.models}
        return (
            orchestrator_model in models
            and worker_model in models
            and _role_model_is_valid(models[orchestrator_model], "orchestrator")
            and _role_model_is_valid(models[worker_model], "worker")
        )
    except (ProviderError, ValueError):
        return False
    finally:
        if adapter:
            await adapter.close()


def _select_menu(title: str, options: list[tuple[str, object]], *, allow_back: bool = True):
    entries = list(options)
    if allow_back:
        entries.append(("Back", None))
    selected_idx = 0
    console.print(f"\n[bold cyan]{title}[/bold cyan]")

    def render(move_up: bool = False) -> None:
        if move_up:
            sys.stdout.write(f"\033[{max(0, len(entries) - 1)}A\033[1G\033[J")
        for index, (label, _) in enumerate(entries):
            marker = ">" if index == selected_idx else " "
            if index == selected_idx:
                line = f"\033[1;36m {marker} {label}\033[0m"
            else:
                line = f" {marker} {label}"
            sys.stdout.write(line)
            if index < len(entries) - 1:
                sys.stdout.write("\n")
        sys.stdout.flush()

    render()
    while True:
        char = get_char()
        if char == "\x1b[A" and selected_idx > 0:
            selected_idx -= 1
        elif char == "\x1b[B" and selected_idx < len(entries) - 1:
            selected_idx += 1
        elif char in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return entries[selected_idx][1]
        elif char == "\x03":
            raise KeyboardInterrupt
        else:
            continue
        render(move_up=True)


def _format_age(fetched_at) -> str:
    if fetched_at is None:
        return "unavailable"
    age = datetime.datetime.now(datetime.timezone.utc) - fetched_at
    return f"{max(0, int(age.total_seconds() // 3600))}h"


async def handle_provider_command(session_name: str, parts: list[str]) -> None:
    if len(parts) < 2:
        console.print("[yellow]Usage: /provider set|switch|assign|list|status[/yellow]")
        return
    action = parts[1].lower()
    config = load_provider_config(session_name)

    if action == "set":
        if len(parts) != 3 or parts[2] not in supported_providers():
            console.print(f"[red]Provider must be one of: {', '.join(supported_providers())}[/red]")
            return
        await _configure_provider(session_name, parts[2], config)
        return

    if action == "assign":
        if len(parts) != 4 or parts[2].lower() not in ROLE_LABELS:
            console.print("[yellow]Usage: /provider assign worker|orchestrator <model-id>[/yellow]")
            return
        await _assign_model(session_name, config, parts[2].lower(), parts[3])
        return

    if action == "switch":
        if len(parts) != 3 or parts[2] not in config.providers:
            console.print("[red]That provider is not configured. Use /provider set <name> first.[/red]")
            return
        config.active_provider = parts[2]
        save_provider_config(session_name, config)
        console.print(f"[green]Active provider switched to {parts[2]}.[/green]")
        return

    if action == "list":
        table = Table(title=f"Configured Providers (Session: {session_name})")
        table.add_column("Provider", style="cyan")
        table.add_column("Active", style="green")
        table.add_column("Key Source", style="yellow")
        for provider, entry in config.providers.items():
            table.add_row(provider, "yes" if provider == config.active_provider else "", key_source(entry, session_name, provider))
        console.print(table if config.providers else "[yellow]No providers configured.[/yellow]")
        return

    if action == "status":
        if not config.active_provider or config.active_provider not in config.providers:
            console.print("[yellow]No active provider configured.[/yellow]")
            return
        provider = config.active_provider
        entry = config.providers[provider]
        adapter = None
        try:
            adapter = create_adapter(provider, resolve_api_key(session_name, provider, entry), entry.base_url)
            cache = await get_models(session_name, provider, adapter)
            orchestrator_model, worker_model = selected_models(config, provider)
            console.print(
                f"[cyan]Provider:[/cyan] {provider}\n"
                f"[cyan]Key source:[/cyan] {key_source(entry, session_name, provider)}\n"
                f"[cyan]Model cache:[/cyan] {cache.source} ({_format_age(cache.fetched_at)})\n"
                f"[cyan]Orchestrator model:[/cyan] {orchestrator_model or 'not selected'}\n"
                f"[cyan]Worker model:[/cyan] {worker_model or 'not selected'}"
            )
        except ProviderError as exc:
            console.print(f"[red]Provider status failed: {exc}[/red]")
        finally:
            if adapter:
                await adapter.close()
        return

    console.print("[yellow]Usage: /provider set|switch|list|status <provider>[/yellow]")


async def _assign_model(session_name: str, config: ProviderConfig, role: str, model_id: str) -> bool:
    if not config.active_provider or config.active_provider not in config.providers:
        console.print("[yellow]Configure a provider first.[/yellow]")
        return False
    provider = config.active_provider
    entry = config.providers[provider]
    adapter = None
    try:
        adapter = create_adapter(provider, resolve_api_key(session_name, provider, entry), entry.base_url)
        result = await get_models(session_name, provider, adapter, role=role)
        model = next((item for item in result.models if item.id == model_id), None)
        if model is None:
            console.print(f"[red]Model '{model_id}' is not available for the {role} role.[/red]")
            return False
        set_selected_model(config, provider, role, model_id)
        save_provider_config(session_name, config)
        console.print(f"[green]{ROLE_LABELS[role]} model set to {model_id}.[/green]")
        return True
    except (ProviderError, ValueError) as exc:
        console.print(f"[red]Model assignment failed: {exc}[/red]")
        return False
    finally:
        if adapter:
            await adapter.close()


async def handle_models_command(session_name: str, parts: list[str]) -> None:
    config = _provider_config_or_error(session_name)
    if config is None:
        return
    provider = config.active_provider
    entry = config.providers[provider]
    try:
        adapter = create_adapter(provider, resolve_api_key(session_name, provider, entry), entry.base_url)
    except (ProviderError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return
    try:
        if len(parts) >= 2 and parts[1].lower() == "use":
            if len(parts) != 4 or parts[2].lower() not in ROLE_LABELS:
                console.print("[yellow]Usage: /models use orchestrator|worker <model-id>[/yellow]")
                return
            return await _assign_model(session_name, config, parts[2].lower(), parts[3])
        if len(parts) >= 2 and parts[1].lower() == "list":
            role = None
            if len(parts) == 4 and parts[2] == "--role" and parts[3].lower() in ROLE_LABELS:
                role = parts[3].lower()
            elif len(parts) != 2:
                console.print("[yellow]Usage: /models list [--role worker|orchestrator][/yellow]")
                return
            result = await get_models(session_name, provider, adapter, role=role)
        elif len(parts) == 2 and parts[1].lower() == "refresh":
            result = await get_models(session_name, provider, adapter, force_refresh=True)
        else:
            console.print("[yellow]Usage: /models list [--role worker|orchestrator], /models refresh, or /models use <role> <model-id>[/yellow]")
            return
        table = Table(title=f"Models for {provider}" + (f" ({role})" if role else "") + f" [{result.source}]")
        for column in ("Model ID", "Display Name", "Context", "Tools", "Thinking"):
            table.add_column(column)
        for model in result.models:
            table.add_row(
                model.id,
                model.display_name,
                str(model.context_window or "unknown"),
                str(model.supports_tools if model.supports_tools is not None else "unknown"),
                str(model.supports_thinking if model.supports_thinking is not None else "unknown"),
            )
        console.print(table if result.models else "[yellow]No models available.[/yellow]")
        if result.warning:
            console.print(f"[yellow]{result.warning}[/yellow]")
    finally:
        await adapter.close()

async def _configure_provider(session_name: str, provider: str, config: ProviderConfig) -> bool:
    configured_key = env_value_for(provider)
    env_name, api_key = configured_key if configured_key else (env_var_for(provider), None)
    if not api_key and provider in config.providers:
        api_key = resolve_api_key(session_name, provider, config.providers[provider])
    adapter = None
    try:
        if not api_key:
            api_key = prompt_key(session_name, provider)
            api_key_ref = "prompt"
        else:
            api_key_ref = f"env:{env_name}" if configured_key else config.providers.get(provider, ProviderEntry("prompt")).api_key_ref
        adapter = create_adapter(provider, api_key)
        await adapter.validate_key()
        result = await get_models(session_name, provider, adapter, force_refresh=True)
        if not result.models:
            console.print(f"[red]Provider '{provider}' returned no usable models.[/red]")
            return False
        config.providers[provider] = ProviderEntry(
            api_key_ref=api_key_ref,
            orchestrator_model=config.providers.get(provider, ProviderEntry(api_key_ref)).orchestrator_model if provider in config.providers else None,
            worker_model=config.providers.get(provider, ProviderEntry(api_key_ref)).worker_model if provider in config.providers else None,
            base_url=config.providers.get(provider, ProviderEntry(api_key_ref)).base_url if provider in config.providers else None,
        )
        config.active_provider = provider
        save_provider_config(session_name, config)
        console.print(f"[green]Configured {provider} with {len(result.models)} available model(s).[/green]")
        return True
    except (ProviderError, ValueError) as exc:
        console.print(f"[red]Provider validation failed: {exc}[/red]")
        return False
    finally:
        if adapter:
            await adapter.close()


async def _select_role_model(session_name: str, config: ProviderConfig, role: str) -> bool:
    provider = config.active_provider
    if not provider or provider not in config.providers:
        console.print("[yellow]Configure a provider first.[/yellow]")
        return False
    entry = config.providers[provider]
    adapter = None
    try:
        adapter = create_adapter(provider, resolve_api_key(session_name, provider, entry), entry.base_url)
        result = await get_models(session_name, provider, adapter, role=role)
        options = [(_model_label(model), model) for model in result.models]
        if result.warning:
            console.print(f"[yellow]{result.warning}[/yellow]")
        if not options:
            console.print(f"[yellow]No models available for the {role} role.[/yellow]")
            return False
        selected = _select_menu(f"Select a model for {role.title()}", options)
        if selected is None:
            return False
        set_selected_model(config, provider, role, selected.id)
        save_provider_config(session_name, config)
        console.print(f"[green]{role.title()} model set to {selected.id}.[/green]")
        return True
    except (ProviderError, ValueError) as exc:
        console.print(f"[red]Model selection failed: {exc}[/red]")
        return False
    finally:
        if adapter:
            await adapter.close()


async def _provider_setup_menu(session_name: str, config: ProviderConfig) -> bool:
    options = []
    for provider in supported_providers():
        marker = []
        if provider in config.providers:
            marker.append("configured")
        if provider == config.active_provider:
            marker.append("active")
        suffix = f"  [{', '.join(marker)}]" if marker else ""
        options.append((f"{provider}{suffix}", provider))
    provider = _select_menu("Select a provider", options)
    if provider is None:
        return False
    if not await _configure_provider(session_name, provider, config):
        return False
    await _select_role_model(session_name, config, "orchestrator")
    await _select_role_model(session_name, config, "worker")
    return True


async def _session_setup_menu(session_name: str) -> bool:
    config = load_provider_config(session_name)
    complete = await _configuration_is_complete(session_name, config)
    while True:
        options = []
        if complete:
            options.append(("Continue with current settings", "continue"))
        options.extend([
            ("Change provider", "provider"),
            ("Change Orchestrator model", "orchestrator"),
            ("Change Worker model", "worker"),
            ("View status", "status"),
        ])
        action = _select_menu("What would you like to do?", options)
        if action is None:
            return complete
        if action == "continue":
            return True
        if action == "provider":
            await _provider_setup_menu(session_name, config)
        elif action in {"orchestrator", "worker"}:
            await _select_role_model(session_name, config, action)
        elif action == "status":
            await handle_provider_command(session_name, ["/provider", "status"])
        complete = await _configuration_is_complete(session_name, config)


async def _startup_setup(session_name: str) -> bool:
    config = load_provider_config(session_name)
    if await _configuration_is_complete(session_name, config):
        provider = config.active_provider
        orchestrator_model, worker_model = selected_models(config, provider)
        console.print(
            f"[cyan]Active:[/cyan] {provider}  |  "
            f"[cyan]Orchestrator:[/cyan] {orchestrator_model}  |  "
            f"[cyan]Worker:[/cyan] {worker_model}"
        )
        choice = input("[Enter] Continue    [c] Change settings: ").strip().lower()
        if choice != "c":
            return True
    return await _session_setup_menu(session_name)


async def handle_undo(db_uri: str, session_name: str) -> None:
    stack = load_rollback_stack(session_name)
    if not stack:
        console.print("[red]Error: No modifications found in rollback stack.[/red]")
        return
        
    last_entry = stack[-1]
    group_id = last_entry.get("commit_group_id", last_entry.get("commit_hash"))
    
    entries_to_undo = []
    while stack and stack[-1].get("commit_group_id", stack[-1].get("commit_hash")) == group_id:
        entries_to_undo.append(stack.pop())
        
    try:
        engine = create_engine(db_uri)
        with engine.begin() as conn:
            for entry in entries_to_undo:
                inverse_queries = generate_inverse_queries(entry)
                for sql, params in inverse_queries:
                    conn.execute(text(sql), params)
                    
        save_rollback_stack(session_name, stack)
        console.print(f"[green]Reverted {len(entries_to_undo)} operation(s) in commit group '{group_id}' successfully.[/green]")
    except Exception as e:
        console.print(f"[red]Error executing rollback: {str(e)}[/red]")
        entries_to_undo.reverse()
        stack.extend(entries_to_undo)
        save_rollback_stack(session_name, stack)

async def handle_log(session_name: str) -> None:
    history = load_query_history(session_name)
    if not history:
        console.print("[yellow]No queries found in history for this session.[/yellow]")
        return

    table = Table(title=f"Query History (Session: {session_name})")
    table.add_column("Hash", style="yellow")
    table.add_column("Timestamp", style="cyan")
    table.add_column("Operation", style="green")
    table.add_column("Table", style="magenta")
    table.add_column("Rows", style="blue", justify="right")
    table.add_column("Rollback", style="red")
    table.add_column("SQL Query", style="dim")

    for entry in reversed(history):
        table.add_row(
            entry.get("commit_hash", "N/A"),
            entry.get("timestamp", "N/A"),
            entry.get("operation", "N/A"),
            entry.get("table", "N/A"),
            str(entry.get("rows_affected", 0)),
            "yes" if entry.get("rollbackable") else "no",
            entry.get("query", "N/A"),
        )

    console.print(table)

async def handle_revert(db_uri: str, session_name: str, target_hash: str) -> None:
    stack = load_rollback_stack(session_name)
    target_idx = -1
    for i, entry in enumerate(stack):
        if entry.get("commit_hash") == target_hash:
            target_idx = i
            break
            
    if target_idx == -1:
        if any(entry.get("commit_hash") == target_hash for entry in load_query_history(session_name)):
            console.print(f"[yellow]Hash '{target_hash}' is a read-only query and cannot be reverted. Use /undo for the latest mutation.[/yellow]")
        else:
            console.print(f"[red]Error: Hash '{target_hash}' not found in mutation history.[/red]")
        return
        
    commits_to_revert = stack[target_idx + 1:]
    if not commits_to_revert:
        console.print(f"[yellow]Database state is already at commit '{target_hash}'. No actions taken.[/yellow]")
        return
        
    try:
        engine = create_engine(db_uri)
        with engine.begin() as conn:
            for entry in reversed(commits_to_revert):
                inverse_queries = generate_inverse_queries(entry)
                for sql, params in inverse_queries:
                    conn.execute(text(sql), params)
                    
        new_stack = stack[:target_idx + 1]
        save_rollback_stack(session_name, new_stack)
        console.print(f"[green]Successfully reverted database state back to commit '{target_hash}'.[/green]")
    except Exception as e:
        console.print(f"[red]Error executing revert: {str(e)}[/red]")

def parse_fallback_tool_calls(content: str) -> List[Dict[str, Any]]:
    if not content:
        return []
    tool_calls = []
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    for block in blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and "name" in data and "arguments" in data:
                tool_calls.append(data)
        except Exception:
            pass
    if not tool_calls:
        try:
            data = json.loads(content.strip())
            if isinstance(data, dict) and "name" in data and "arguments" in data:
                tool_calls.append(data)
        except Exception:
            pass
    if not tool_calls:
        matches = re.finditer(r"\{\s*\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"arguments\"\s*:\s*\{.*?\}\s*\}", content, re.DOTALL)
        for match in matches:
            try:
                data = json.loads(match.group(0))
                tool_calls.append(data)
            except Exception:
                pass
    return tool_calls

async def run_chat_loop(db_uri: str, session_name: str, mcp_session: ClientSession):
    from db_agent.agents.orchestrator import plan_dag
    from db_agent.agents.worker import execute_task

    init_session(session_name)
    chat_messages = load_chat_memory(session_name)
    if not await _startup_setup(session_name):
        console.print("[cyan]Session setup cancelled.[/cyan]")
        return
    print_banner(db_uri, session_name)

    while True:
        user_query = await asyncio.get_event_loop().run_in_executor(None, get_user_input)
        user_query = user_query.strip()
        
        if not user_query:
            continue

        if user_query == "/":
            command = _slash_command_menu()
            if command:
                user_query = command
            else:
                continue
            
        if user_query in ("/exit", "exit"):
            console.print("[cyan]Exiting db-agent session. Goodbye![/cyan]")
            break
            
        elif user_query == "/undo":
            await handle_undo(db_uri, session_name)
            continue
            
        elif user_query == "/log":
            await handle_log(session_name)
            continue
            
        elif user_query.startswith("/revert"):
            parts = user_query.split()
            if len(parts) < 2:
                console.print("[red]Error: Please specify a commit hash (e.g., /revert abc12345)[/red]")
            else:
                await handle_revert(db_uri, session_name, parts[1])
            continue

        elif user_query == "/providers":
            await _session_setup_menu(session_name)
            continue

        elif user_query.startswith("/provider"):
            await handle_provider_command(session_name, user_query.split())
            continue

        elif user_query.startswith("/models"):
            await handle_models_command(session_name, user_query.split())
            continue

        chat_messages.append({"role": "user", "content": user_query})

        config = _provider_config_or_error(session_name)
        if config is None:
            continue
        provider = config.active_provider
        entry = config.providers[provider]
        orchestrator_model, worker_model = selected_models(config, provider)
        if not orchestrator_model or not worker_model:
            console.print("[yellow]Choose both models before running a request: /models use orchestrator <id> and /models use worker <id>.[/yellow]")
            continue
        try:
            adapter = create_adapter(provider, resolve_api_key(session_name, provider, entry), entry.base_url)
        except (ProviderError, ValueError) as exc:
            console.print(f"[red]Provider setup error: {exc}[/red]")
            continue
        
        try:
            schema_resource = await mcp_session.read_resource("schema://current")
            schema_text = schema_resource.contents[0].text
        except Exception as e:
            schema_text = f"Could not reflect database schema: {str(e)}"
            
        with console.status("[bold green]Orchestrating tasks..."):
            dag = await plan_dag(user_query, schema_text, adapter, orchestrator_model)
            
        if not dag:
            console.print("[red]Failed to generate an execution plan for this query.[/red]")
            continue
            
        console.print(f"[cyan]Generated DAG with {len(dag)} atomic operation(s).[/cyan]")
        
        group_id = secrets.token_hex(4)
        
        completed = set()
        task_results = []
        
        while len(completed) < len(dag):
            runnable_tasks = [t for t in dag if t["id"] not in completed and all(dep in completed for dep in t.get("depends_on", []))]
            
            if not runnable_tasks:
                console.print("[red]Error: Circular dependency or unresolvable tasks in DAG.[/red]")
                break
                
            async def run_worker(task):
                console.print(f"[yellow]Running Task {task['id']}: {task.get('intent')}[/yellow]")
                result = await execute_task(task, schema_text, group_id, mcp_session, adapter, worker_model)
                if result["status"] == "success":
                    console.print(f"[green]Task {task['id']} completed.[/green]")
                else:
                    console.print(f"[red]Task {task['id']} failed: {result['result']}[/red]")
                return task["id"], result
                
            batch_results = await asyncio.gather(*(run_worker(t) for t in runnable_tasks))
            
            has_failure = False
            for tid, result in batch_results:
                task_results.append(f"Task {tid}: {result['result']}")
                if result["status"] != "success":
                    has_failure = True
                completed.add(tid)
                
            if has_failure:
                console.print("[red]Aborting remaining DAG execution due to task failure.[/red]")
                break
                
        summary = "\n".join(task_results)
        console.print(Panel(Markdown("Execution complete:\n" + summary), border_style="green", title="db-agent"))
        chat_messages.append({"role": "assistant", "content": f"Executed DAG.\nResults:\n{summary}"})
        save_chat_memory(session_name, [m for m in chat_messages if m.get("role") != "system"])
        await adapter.close()

def get_char() -> str:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            ch3 = sys.stdin.read(1)
            return ch + ch2 + ch3
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def _delete_session_with_confirmation(session_name: str) -> bool:
    confirm = input(f"\nAre you sure you want to delete session '{session_name}'? (y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[yellow]Deletion cancelled.[/yellow]")
        return False
    try:
        shutil.rmtree(get_session_dir(session_name))
        console.print(f"[green]Deleted session '{session_name}' successfully.[/green]")
        return True
    except FileNotFoundError:
        console.print(f"[yellow]Session '{session_name}' was already absent.[/yellow]")
    except OSError as exc:
        console.print(f"[red]Error deleting session folder: {exc}[/red]")
    return False


def _choose_session_to_delete() -> str | None:
    sessions = get_all_sessions()
    if not sessions:
        console.print("[yellow]No sessions available to delete.[/yellow]")
        return None
    options = [(f"{session['name']} (DB: {session['db_uri']})", session["name"]) for session in sessions]
    return _select_menu("Select a session to delete", options)


def interactive_session_menu() -> tuple[str, str] | None:
    console.print(Panel("[bold cyan]Welcome to db-agent CLI[/bold cyan]", border_style="cyan", expand=False))
    
    while True:
        sessions = get_all_sessions()
        
        menu_options = []
        for s in sessions:
            menu_options.append({
                "is_new": False,
                "is_exit": False,
                "name": s["name"],
                "db_uri": s["db_uri"],
                "last_active": s["last_active"]
            })
        menu_options.append({
            "is_new": True,
            "is_exit": False,
            "name": "",
            "db_uri": "",
            "last_active": ""
        })
        menu_options.append({
            "is_new": False,
            "is_delete": True,
            "is_exit": False,
            "name": "",
            "db_uri": "",
            "last_active": ""
        })
        menu_options.append({
            "is_new": False,
            "is_delete": False,
            "is_exit": True,
            "name": "",
            "db_uri": "",
            "last_active": ""
        })
        
        selected_idx = 0
        
        def print_menu(move_up: bool = False):
            if move_up:
                sys.stdout.write(f"\033[{max(0, len(menu_options) - 1)}A\033[1G\033[J")
            for i, opt in enumerate(menu_options):
                marker = ">" if i == selected_idx else " "
                if opt.get("is_new"):
                    line = f" {marker} Start a new session"
                elif opt.get("is_delete"):
                    line = f" {marker} Delete a session"
                elif opt.get("is_exit"):
                    line = f" {marker} Exit db-agent"
                else:
                    line = f" {marker} {opt['name']} (DB: {opt['db_uri']}) - Active: {opt['last_active']}"
                if i == selected_idx:
                    line = f"\033[1;36m{line}\033[0m"
                sys.stdout.write(line)
                if i < len(menu_options) - 1:
                    sys.stdout.write("\n")
            sys.stdout.flush()
                
        console.print("\n[bold]Arrow keys (Up/Down) to choose, 'd' to delete selected, Enter to select:[/bold]")
        print_menu()
        
        while True:
            char = get_char()
            if char == '\x1b[A':
                if selected_idx > 0:
                    selected_idx -= 1
                    print_menu(move_up=True)
            elif char == '\x1b[B':
                if selected_idx < len(menu_options) - 1:
                    selected_idx += 1
                    print_menu(move_up=True)
            elif char in ('d', 'D'):
                opt = menu_options[selected_idx]
                if not opt["is_new"] and not opt.get("is_delete") and not opt.get("is_exit"):
                    _delete_session_with_confirmation(opt["name"])
                    break
            elif char in ('\r', '\n'):
                selected_opt = menu_options[selected_idx]
                if selected_opt.get("is_delete"):
                    session_to_delete = _choose_session_to_delete()
                    if session_to_delete:
                        _delete_session_with_confirmation(session_to_delete)
                    break
                selected_opt = menu_options[selected_idx]
                if selected_opt.get("is_exit"):
                    return None
                elif not selected_opt["is_new"]:
                    return selected_opt["name"], selected_opt["db_uri"]
                else:
                    return prompt_new_session()
            elif char == '\x03':
                raise KeyboardInterrupt

def prompt_new_session() -> tuple[str, str]:
    while True:
        session_name = input("Enter new session name: ").strip()
        if session_name:
            if re.match(r"^\w+$", session_name):
                break
            console.print("[red]Invalid name. Please use alphanumeric characters and underscores only.[/red]")
            
    while True:
        db_uri = input("Enter database URI (e.g., sqlite:///test.db): ").strip()
        if db_uri:
            break
            
    save_session_config(session_name, db_uri)
    return session_name, db_uri

async def run_cli():
    parser = argparse.ArgumentParser(description="db-agent: Autonomous CLI Database Assistant")
    parser.add_argument("db_uri", nargs="?", help="SQL Connection URI (e.g. sqlite:///test.db)")
    parser.add_argument("--session", default=None, help="Session name for workspace isolation")
    args = parser.parse_args()
    
    db_uri = args.db_uri
    session_name = args.session
    is_interactive = not db_uri
    
    while True:
        if is_interactive:
            menu_res = interactive_session_menu()
            if menu_res is None:
                console.print("[cyan]Exiting db-agent. Goodbye![/cyan]")
                break
            session_name, db_uri = menu_res
        else:
            if not session_name:
                session_name = "default"
            save_session_config(session_name, db_uri)
            
        if not db_uri or db_uri == "Unknown":
            console.print(f"[yellow]Session '{session_name}' does not have a configured database URI.[/yellow]")
            while True:
                db_uri = input("Enter database URI (e.g., sqlite:///test.db): ").strip()
                if db_uri:
                    break
            save_session_config(session_name, db_uri)
            
        if not await check_dependencies(db_uri):
            if is_interactive:
                continue
            else:
                sys.exit(1)
            
        os.makedirs(".db_agent", exist_ok=True)
        with open(os.path.join(".db_agent", "active_session.txt"), "w", encoding="utf-8") as f:
            f.write(session_name)
            
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "db_agent.mcp_server"],
            env={
                **os.environ,
                "DYNAMIC_DB_URI": db_uri,
                "SESSION_NAME": session_name
            }
        )
        
        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    await run_chat_loop(db_uri, session_name, session)
        except Exception as e:
            console.print(f"[red]MCP Session Subprocess Error: {str(e)}[/red]")
            if not is_interactive:
                sys.exit(1)
                
        if not is_interactive:
            break

def main():
    load_dotenv()
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        console.print("\n[yellow]Session terminated by user.[/yellow]")
        sys.exit(0)

if __name__ == "__main__":
    main()
