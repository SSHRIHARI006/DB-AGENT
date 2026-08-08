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
    save_rollback_stack,
    generate_inverse_queries,
    save_session_config,
    load_session_config,
    get_all_sessions,
)
from db_agent.model_cache import get_models
from db_agent.provider_config import (
    ProviderConfig,
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
    banner_text = (
        f"# db-agent CLI\n\n"
        f"**Database URI:** `{db_uri}`\n"
        f"**Active Session:** `{session_name}`\n\n"
        f"Ready to accept database requests in natural language.\n"
        f"**Special Commands:**\n"
        f"- `/undo`        : Revert the last database mutation operation.\n"
        f"- `/log`         : Display the local commit history timeline.\n"
        f"- `/revert <hash>`: Sequentially rollback to a specific commit hash.\n"
        f"- `/exit` or `exit`: Safely shut down and quit the application."
    )
    console.print(Panel(Markdown(banner_text), border_style="cyan", expand=False))

def get_user_input() -> str:
    try:
        return input("\ndb-agent> ")
    except (KeyboardInterrupt, EOFError):
        return "/exit"

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


def _format_age(fetched_at) -> str:
    if fetched_at is None:
        return "unavailable"
    age = datetime.datetime.now(datetime.timezone.utc) - fetched_at
    return f"{max(0, int(age.total_seconds() // 3600))}h"


async def handle_provider_command(session_name: str, parts: list[str]) -> None:
    if len(parts) < 2:
        console.print("[yellow]Usage: /provider set|switch|list|status <provider>[/yellow]")
        return
    action = parts[1].lower()
    config = load_provider_config(session_name)

    if action == "set":
        if len(parts) != 3 or parts[2] not in supported_providers():
            console.print(f"[red]Provider must be one of: {', '.join(supported_providers())}[/red]")
            return
        provider = parts[2]
        configured_key = env_value_for(provider)
        env_name, api_key = configured_key if configured_key else (env_var_for(provider), None)
        from_environment = configured_key is not None
        adapter = None
        try:
            if not api_key:
                api_key = prompt_key(session_name, provider)
            adapter = create_adapter(provider, api_key)
            await adapter.validate_key()
            model_result = await get_models(session_name, provider, adapter, force_refresh=True)
            if not model_result.models:
                console.print(f"[red]Provider '{provider}' returned no usable models.[/red]")
                return
            from db_agent.provider_config import ProviderEntry
            config.providers[provider] = ProviderEntry(
                api_key_ref=f"env:{env_name}" if from_environment else "prompt"
            )
            if not config.active_provider:
                config.active_provider = provider
            save_provider_config(session_name, config)
            console.print(f"[green]Configured {provider} with {len(model_result.models)} available model(s).[/green]")
            console.print(f"[cyan]Assign roles with /models use orchestrator <id> and /models use worker <id>.[/cyan]")
            if model_result.warning:
                console.print(f"[yellow]{model_result.warning}[/yellow]")
        except ProviderError as exc:
            console.print(f"[red]Provider validation failed: {exc}[/red]")
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
        finally:
            if adapter:
                await adapter.close()
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
            model_id = parts[3]
            result = await get_models(session_name, provider, adapter)
            if not any(model.id == model_id for model in result.models):
                console.print(f"[red]Model '{model_id}' is not available for {provider}.[/red]")
                return
            set_selected_model(config, provider, parts[2].lower(), model_id)
            save_provider_config(session_name, config)
            console.print(f"[green]{ROLE_LABELS[parts[2].lower()]} model set to {model_id}.[/green]")
            return
        if len(parts) != 2 or parts[1].lower() not in {"list", "refresh"}:
            console.print("[yellow]Usage: /models list|refresh or /models use orchestrator|worker <model-id>[/yellow]")
            return
        result = await get_models(session_name, provider, adapter, force_refresh=parts[1].lower() == "refresh")
        table = Table(title=f"Models for {provider} ({result.source})")
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
    stack = load_rollback_stack(session_name)
    if not stack:
        console.print("[yellow]No commits found in rollback stack for this session.[/yellow]")
        return
        
    table = Table(title=f"Commit Log History (Session: {session_name})")
    table.add_column("Commit Hash", style="yellow")
    table.add_column("Timestamp", style="cyan")
    table.add_column("Operation", style="green")
    table.add_column("Table", style="magenta")
    table.add_column("Rows", style="blue", justify="right")
    table.add_column("SQL Query", style="dim")
    
    for entry in reversed(stack):
        table.add_row(
            entry.get("commit_hash", "N/A"),
            entry.get("timestamp", "N/A"),
            entry.get("operation", "N/A"),
            entry.get("table", "N/A"),
            str(len(entry.get("before", []))),
            entry.get("query", "N/A")
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
        console.print(f"[red]Error: Commit hash '{target_hash}' not found in the stack.[/red]")
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
    print_banner(db_uri, session_name)

    while True:
        user_query = await asyncio.get_event_loop().run_in_executor(None, get_user_input)
        user_query = user_query.strip()
        
        if not user_query:
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
            "is_exit": True,
            "name": "",
            "db_uri": "",
            "last_active": ""
        })
        
        selected_idx = 0
        
        def print_menu():
            for i, opt in enumerate(menu_options):
                marker = ">" if i == selected_idx else " "
                style = "[bold cyan]" if i == selected_idx else ""
                end_style = "[/bold cyan]" if i == selected_idx else ""
                
                if opt.get("is_new"):
                    line = f" {marker} {style}Start a new session{end_style}"
                elif opt.get("is_exit"):
                    line = f" {marker} {style}Exit db-agent{end_style}"
                else:
                    line = f" {marker} {style}{opt['name']} (DB: {opt['db_uri']}) - Active: {opt['last_active']}{end_style}"
                
                console.print(f"\r\x1b[K{line}", end="\n")
                
        console.print("\n[bold]Arrow keys (Up/Down) to choose, 'd' to delete selected, Enter to select:[/bold]")
        print_menu()
        
        while True:
            char = get_char()
            if char == '\x1b[A':
                if selected_idx > 0:
                    selected_idx -= 1
                    sys.stdout.write(f"\x1b[{len(menu_options)}A")
                    print_menu()
            elif char == '\x1b[B':
                if selected_idx < len(menu_options) - 1:
                    selected_idx += 1
                    sys.stdout.write(f"\x1b[{len(menu_options)}A")
                    print_menu()
            elif char in ('d', 'D'):
                opt = menu_options[selected_idx]
                if not opt["is_new"] and not opt.get("is_exit"):
                    session_to_delete = opt["name"]
                    confirm = input(f"\nAre you sure you want to delete session '{session_to_delete}'? (y/n): ").strip().lower()
                    if confirm in ('y', 'yes'):
                        from db_agent.tracker import get_session_dir
                        session_dir = get_session_dir(session_to_delete)
                        try:
                            shutil.rmtree(session_dir)
                            console.print(f"[green]Deleted session '{session_to_delete}' successfully.[/green]")
                        except Exception as e:
                            console.print(f"[red]Error deleting session folder: {str(e)}[/red]")
                    else:
                        console.print("[yellow]Deletion cancelled.[/yellow]")
                    break
            elif char in ('\r', '\n'):
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
