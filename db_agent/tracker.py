import os
import json
import datetime
import secrets
from typing import List, Dict, Any, Tuple

def get_session_dir(session_name: str) -> str:
    session_dir = os.path.abspath(os.path.join(".db_agent", "sessions", session_name))
    os.makedirs(session_dir, exist_ok=True)
    return session_dir

def init_session(session_name: str) -> None:
    session_dir = get_session_dir(session_name)
    
    chat_memory_path = os.path.join(session_dir, "chat_memory.json")
    if not os.path.exists(chat_memory_path):
        with open(chat_memory_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
            
    rollback_stack_path = os.path.join(session_dir, "rollback_stack.json")
    if not os.path.exists(rollback_stack_path):
        with open(rollback_stack_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

    query_history_path = os.path.join(session_dir, "query_history.json")
    if not os.path.exists(query_history_path):
        try:
            with open(rollback_stack_path, "r", encoding="utf-8") as f:
                rollback_entries = json.load(f)
        except (OSError, ValueError, TypeError):
            rollback_entries = []
        history_entries = [
            {
                "commit_hash": entry.get("commit_hash"),
                "commit_group_id": entry.get("commit_group_id"),
                "timestamp": entry.get("timestamp"),
                "operation": entry.get("operation", "MUTATION"),
                "table": entry.get("table", "N/A"),
                "query": entry.get("query", ""),
                "rows_affected": len(entry.get("before", [])),
                "rollbackable": True,
            }
            for entry in rollback_entries
        ]
        with open(query_history_path, "w", encoding="utf-8") as f:
            json.dump(history_entries, f, indent=2)
            
    history_md_path = os.path.join(session_dir, "DB_HISTORY.md")
    if not os.path.exists(history_md_path):
        with open(history_md_path, "w", encoding="utf-8") as f:
            f.write(f"# DB Agent Operation History Ledger\n\nSession: `{session_name}`\nInitialized: {datetime.datetime.now().isoformat()}\n\n---\n")

def load_chat_memory(session_name: str) -> List[Dict[str, Any]]:
    session_dir = get_session_dir(session_name)
    chat_memory_path = os.path.join(session_dir, "chat_memory.json")
    try:
        with open(chat_memory_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_chat_memory(session_name: str, messages: List[Dict[str, Any]]) -> None:
    session_dir = get_session_dir(session_name)
    chat_memory_path = os.path.join(session_dir, "chat_memory.json")
    with open(chat_memory_path, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2)

def load_rollback_stack(session_name: str) -> List[Dict[str, Any]]:
    session_dir = get_session_dir(session_name)
    rollback_stack_path = os.path.join(session_dir, "rollback_stack.json")
    try:
        with open(rollback_stack_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_rollback_stack(session_name: str, stack: List[Dict[str, Any]]) -> None:
    session_dir = get_session_dir(session_name)
    rollback_stack_path = os.path.join(session_dir, "rollback_stack.json")
    with open(rollback_stack_path, "w", encoding="utf-8") as f:
        json.dump(stack, f, indent=2)

def load_query_history(session_name: str) -> List[Dict[str, Any]]:
    init_session(session_name)
    history_path = os.path.join(get_session_dir(session_name), "query_history.json")
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return []


def save_query_history(session_name: str, history: List[Dict[str, Any]]) -> None:
    history_path = os.path.join(get_session_dir(session_name), "query_history.json")
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def add_query_history_entry(
    session_name: str,
    operation: str,
    table: str,
    query: str,
    rows_affected: int = 0,
    *,
    commit_hash: str | None = None,
    commit_group_id: str | None = None,
    rollbackable: bool = False,
) -> str:
    init_session(session_name)
    history = load_query_history(session_name)
    query_hash = commit_hash or secrets.token_hex(4)
    entry = {
        "commit_hash": query_hash,
        "commit_group_id": commit_group_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "operation": operation.upper(),
        "table": table,
        "query": query,
        "rows_affected": rows_affected,
        "rollbackable": rollbackable,
    }
    history.append(entry)
    save_query_history(session_name, history)
    append_history_log(session_name, query_hash, operation, table, query, rows_affected, commit_group_id)
    return query_hash


def add_rollback_entry(
    session_name: str,
    operation: str,
    table: str,
    query: str,
    before: List[Dict[str, Any]],
    primary_keys: List[str],
    commit_group_id: str = None
) -> str:
    init_session(session_name)
    stack = load_rollback_stack(session_name)
    
    commit_hash = secrets.token_hex(4)
    timestamp = datetime.datetime.now().isoformat()
    
    entry = {
        "commit_hash": commit_hash,
        "commit_group_id": commit_group_id or commit_hash,
        "timestamp": timestamp,
        "operation": operation.upper(),
        "table": table,
        "query": query,
        "before": before,
        "primary_keys": primary_keys
    }
    
    stack.append(entry)
    save_rollback_stack(session_name, stack)
    
    add_query_history_entry(
        session_name,
        operation,
        table,
        query,
        len(before),
        commit_hash=commit_hash,
        commit_group_id=commit_group_id or commit_hash,
        rollbackable=True,
    )
    
    return commit_hash

def append_history_log(
    session_name: str,
    commit_hash: str,
    operation: str,
    table: str,
    query: str,
    row_count: int,
    commit_group_id: str = None
) -> None:
    session_dir = get_session_dir(session_name)
    history_md_path = os.path.join(session_dir, "DB_HISTORY.md")
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    group_str = f" (Group: `{commit_group_id}`)" if commit_group_id else ""
    log_entry = (
        f"\n### Commit `{commit_hash}`{group_str}\n"
        f"- **Timestamp:** {timestamp}\n"
        f"- **Operation:** {operation.upper()}\n"
        f"- **Table:** `{table}`\n"
        f"- **Rows Impacted:** {row_count}\n"
        f"- **Raw Query:**\n"
        f"  ```sql\n"
        f"  {query}\n"
        f"  ```\n"
        f"  \n"
        f"---"
    )
    
    with open(history_md_path, "a", encoding="utf-8") as f:
        f.write(log_entry)

def generate_inverse_queries(entry: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    operation = entry.get("operation", "").upper()
    table = entry.get("table", "")
    before = entry.get("before", [])
    primary_keys = entry.get("primary_keys", [])
    
    inverse_queries = []
    
    if not table:
        return []
        
    if operation == "UPDATE":
        for idx, row in enumerate(before):
            if not row:
                continue
                
            pks = primary_keys if primary_keys else (["id"] if "id" in row else list(row.keys()))
            
            set_clauses = []
            where_clauses = []
            params = {}
            
            for key, val in row.items():
                param_name = f"{key}_{idx}"
                
                if key in pks:
                    if val is None:
                        where_clauses.append(f"{key} IS NULL")
                    else:
                        where_clauses.append(f"{key} = :{param_name}")
                        params[param_name] = val
                else:
                    set_clauses.append(f"{key} = :{param_name}")
                    params[param_name] = val
            
            if not set_clauses:
                for key in row.keys():
                    param_name = f"{key}_{idx}"
                    set_clauses.append(f"{key} = :{param_name}")
                    params[param_name] = row[key]
                    
            sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
            inverse_queries.append((sql, params))
            
    elif operation == "DELETE":
        for idx, row in enumerate(before):
            if not row:
                continue
            cols = list(row.keys())
            placeholders = []
            params = {}
            for col in cols:
                param_name = f"{col}_{idx}"
                placeholders.append(f":{param_name}")
                params[param_name] = row[col]
                
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
            inverse_queries.append((sql, params))
            
    elif operation == "INSERT":
        for idx, row in enumerate(before):
            if not row:
                continue
            
            pks = primary_keys if primary_keys else (["id"] if "id" in row else list(row.keys()))
            where_clauses = []
            params = {}
            
            for pk in pks:
                if pk in row:
                    val = row[pk]
                    if val is None:
                        where_clauses.append(f"{pk} IS NULL")
                    else:
                        param_name = f"{pk}_{idx}"
                        where_clauses.append(f"{pk} = :{param_name}")
                        params[param_name] = val
            
            if not where_clauses:
                for col, val in row.items():
                    if val is None:
                        where_clauses.append(f"{col} IS NULL")
                    else:
                        param_name = f"{col}_{idx}"
                        where_clauses.append(f"{col} = :{param_name}")
                        params[param_name] = val
                    
            sql = f"DELETE FROM {table} WHERE {' AND '.join(where_clauses)}"
            inverse_queries.append((sql, params))
            
    return inverse_queries

def save_session_config(session_name: str, db_uri: str) -> None:
    session_dir = get_session_dir(session_name)
    config_path = os.path.join(session_dir, "session_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"db_uri": db_uri}, f, indent=2)

def load_session_config(session_name: str) -> str:
    session_dir = get_session_dir(session_name)
    config_path = os.path.join(session_dir, "session_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("db_uri")
    except Exception:
        return None

def get_all_sessions() -> List[Dict[str, Any]]:
    sessions_root = os.path.abspath(os.path.join(".db_agent", "sessions"))
    if not os.path.exists(sessions_root):
        return []
    
    sessions = []
    for entry in os.listdir(sessions_root):
        entry_path = os.path.join(sessions_root, entry)
        if os.path.isdir(entry_path):
            config_path = os.path.join(entry_path, "session_config.json")
            db_uri = "Unknown"
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        db_uri = json.load(f).get("db_uri", "Unknown")
                except Exception:
                    pass
            
            last_active = "Unknown"
            chat_mem_path = os.path.join(entry_path, "chat_memory.json")
            target_path = chat_mem_path if os.path.exists(chat_mem_path) else config_path
            if os.path.exists(target_path):
                mtime = os.path.getmtime(target_path)
                last_active = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                
            sessions.append({
                "name": entry,
                "db_uri": db_uri,
                "last_active": last_active
            })
            
    sessions.sort(key=lambda s: s["last_active"], reverse=True)
    return sessions
