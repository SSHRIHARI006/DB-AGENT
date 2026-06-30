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
from typing import List, Dict, Any

import ollama
from sqlalchemy import create_engine, text
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

from db_agent.tracker import (
    init_session,
    load_chat_memory,
    save_chat_memory,
    load_rollback_stack,
    save_rollback_stack,
    generate_inverse_queries,
    save_session_config,
    load_session_config,
    get_all_sessions
)

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
    except Exception as e:
        console.print(f"[red]Database Connection Error: Cannot connect using the provided URI.[/red]")
        console.print(f"[red]Detail: {str(e)}[/red]")
        return False

    try:
        client = ollama.AsyncClient(host="http://localhost:11434")
        models_response = await client.list()
        pulled_models = [m.model for m in models_response.models]
        
        target_model = "qwen2.5-coder:1.5b"
        if not any(target_model in m or "qwen2.5-coder" in m for m in pulled_models):
            console.print(f"[yellow]Warning: Model '{target_model}' was not found in local Ollama daemon.[/yellow]")
            console.print(f"[yellow]Please run 'ollama pull {target_model}' to ensure responses work correctly.[/yellow]")
    except Exception as e:
        console.print("[red]Ollama Connection Error: Cannot communicate with Ollama daemon at http://localhost:11434.[/red]")
        console.print("[yellow]Please ensure Ollama is installed and running (`ollama serve`).[/yellow]")
        return False
        
    return True

async def handle_undo(db_uri: str, session_name: str) -> None:
    stack = load_rollback_stack(session_name)
    if not stack:
        console.print("[red]Error: No modifications found in rollback stack.[/red]")
        return
        
    last_entry = stack.pop()
    inverse_queries = generate_inverse_queries(last_entry)
    
    if not inverse_queries:
        console.print("[yellow]Warning: No compensating queries generated for the last commit.[/yellow]")
        save_rollback_stack(session_name, stack)
        return
        
    try:
        engine = create_engine(db_uri)
        with engine.begin() as conn:
            for sql, params in inverse_queries:
                conn.execute(text(sql), params)
                
        save_rollback_stack(session_name, stack)
        console.print(f"[green]Reverted last {last_entry['operation']} on table '{last_entry['table']}' successfully.[/green]")
    except Exception as e:
        console.print(f"[red]Error executing rollback: {str(e)}[/red]")

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
    ollama_client = ollama.AsyncClient(host="http://localhost:11434")
    model_name = "qwen2.5-coder:1.5b"
    
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
            
        chat_messages.append({"role": "user", "content": user_query})
        
        try:
            schema_resource = await mcp_session.read_resource("schema://current")
            schema_text = schema_resource.contents[0].text
        except Exception as e:
            schema_text = f"Could not reflect database schema: {str(e)}"
            
        stack = load_rollback_stack(session_name)
        if stack:
            history_lines = []
            for entry in stack:
                history_lines.append(f"- Commit {entry['commit_hash']}: {entry['operation']} on table '{entry['table']}' (Query: {entry['query']})")
            history_text = "\n".join(history_lines)
        else:
            history_text = "No database mutations have been recorded in this session yet."
            
        system_prompt = (
            "You are an autonomous CLI database assistant. Your goal is to help the user manage "
            "their SQL database using natural language. You have access to tools to read and write to the database.\n\n"
            "CRITICAL RULES:\n"
            "1. If the user asks a general question, conversational query, or meta-question about the conversation/session history (e.g. 'what have we done', 'tell me about the last operation', or general greeting), "
            "answer it directly using the context and history. Do NOT call tools for conversational queries.\n"
            "2. Read-only queries (SELECT) must use the `read_query` tool.\n"
            "3. Mutations (INSERT, UPDATE, DELETE) must use `execute_smart_mutation`.\n"
            "4. Do NOT execute structural changes (DDL) like CREATE TABLE, DROP, or ALTER. Schema modifications are blocked. If the user asks to create a table, explain that they must create the table manually using their own SQL tool first.\n"
            "5. Always formulate the correct query using the database schema provided below.\n\n"
            f"CURRENT DATABASE SCHEMA:\n{schema_text}\n\n"
            f"RECORDED MUTATIONS IN THIS SESSION:\n{history_text}"
        )
        
        chat_messages = [msg for msg in chat_messages if msg.get("role") != "system"]
        chat_messages.insert(0, {"role": "system", "content": system_prompt})
        
        mcp_tools = await mcp_session.list_tools()
        ollama_tools = []
        for t in mcp_tools.tools:
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema
                }
            })
            
        valid_tool_names = [t.name for t in mcp_tools.tools]
        executed_calls_in_turn = set()
        loop_count = 0
        max_loops = 5
        
        while True:
            if loop_count >= max_loops:
                console.print("[yellow]Warning: Maximum agent tool call depth reached. Breaking loop to prevent infinite run.[/yellow]")
                break
                
            with console.status("[bold green]Thinking...") as status:
                try:
                    response = await ollama_client.chat(
                        model=model_name,
                        messages=chat_messages,
                        tools=ollama_tools
                    )
                except Exception as chat_err:
                    console.print(f"[red]Ollama Error: {str(chat_err)}[/red]")
                    break
                    
            msg = response.message
            
            calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tname = tc.function.name
                    if tname in valid_tool_names:
                        call_id = getattr(tc, "id", None) or f"call_{secrets.token_hex(4)}"
                        calls.append({
                            "id": call_id,
                            "name": tname,
                            "arguments": tc.function.arguments
                        })
                    else:
                        console.print(f"[yellow]Ignored invalid tool call '{tname}' suggested by model.[/yellow]")
            else:
                fallback_calls = parse_fallback_tool_calls(msg.content)
                if fallback_calls:
                    for c in fallback_calls:
                        tname = c["name"]
                        if tname in valid_tool_names:
                            calls.append({
                                "id": f"call_{secrets.token_hex(4)}",
                                "name": tname,
                                "arguments": c["arguments"]
                            })
                        else:
                            console.print(f"[yellow]Ignored invalid fallback tool call '{tname}' suggested by model.[/yellow]")
            
            if calls:
                loop_count += 1
                assistant_msg = {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": c["arguments"]
                            }
                        } for c in calls
                    ]
                }
                chat_messages.append(assistant_msg)
                
                for tool_call in calls:
                    name = tool_call["name"]
                    args = tool_call["arguments"]
                    
                    try:
                        args_str = json.dumps(args, sort_keys=True)
                    except Exception:
                        args_str = str(args)
                    call_key = (name, args_str)
                    
                    if call_key in executed_calls_in_turn:
                        console.print(f"[yellow]Bypassed repeating tool call '{name}' to prevent loop.[/yellow]")
                        chat_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": name,
                            "content": f"Error: The tool call '{name}' with these arguments was already executed and failed. Do not repeat the same call. Explain the issue (e.g., table does not exist) to the user."
                        })
                        continue
                        
                    executed_calls_in_turn.add(call_key)
                    console.print(f"[cyan]Agent executing: {name} (Args: {args})[/cyan]")
                    
                    try:
                        tool_result = await mcp_session.call_tool(name, args)
                        
                        result_text = ""
                        if hasattr(tool_result, "content"):
                            for block in tool_result.content:
                                if block.type == "text":
                                    result_text += block.text
                                else:
                                    result_text += str(block)
                        else:
                            result_text = str(tool_result)
                            
                        chat_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": name,
                            "content": result_text
                        })
                    except Exception as tool_err:
                        console.print(f"[red]Tool error: {str(tool_err)}[/red]")
                        chat_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": name,
                            "content": f"Error: {str(tool_err)}"
                        })
                continue
            else:
                console.print(Panel(Markdown(msg.content or ""), border_style="green", title="db-agent"))
                chat_messages.append({
                    "role": "assistant",
                    "content": msg.content or ""
                })
                save_chat_memory(session_name, [m for m in chat_messages if m.get("role") != "system"])
                break

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
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        console.print("\n[yellow]Session terminated by user.[/yellow]")
        sys.exit(0)

if __name__ == "__main__":
    main()
