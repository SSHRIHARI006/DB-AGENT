import json

from db_agent import mcp_server
from db_agent.tracker import (
    add_query_history_entry,
    add_rollback_entry,
    init_session,
    load_query_history,
)


def test_query_history_persists_read_hashes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    first = add_query_history_entry("session", "SELECT", "query", "SELECT 1", 1)
    second = add_query_history_entry("session", "SELECT", "query", "SELECT 2", 1)
    history = load_query_history("session")
    assert [entry["commit_hash"] for entry in history] == [first, second]
    assert first != second
    assert all(entry["rollbackable"] is False for entry in history)


def test_mutation_hash_is_shared_with_query_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    commit_hash = add_rollback_entry(
        "session",
        "INSERT",
        "products",
        "INSERT INTO products VALUES (1, 'x', 1, 1)",
        [{"id": 1}],
        ["id"],
    )
    history = load_query_history("session")
    assert history[-1]["commit_hash"] == commit_hash
    assert history[-1]["rollbackable"] is True


def test_existing_rollback_stack_is_migrated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session_dir = tmp_path / ".db_agent" / "sessions" / "session"
    session_dir.mkdir(parents=True)
    entry = {
        "commit_hash": "abc12345",
        "commit_group_id": "abc12345",
        "timestamp": "2026-01-01T00:00:00",
        "operation": "INSERT",
        "table": "products",
        "query": "INSERT ...",
        "before": [{"id": 1}],
        "primary_keys": ["id"],
    }
    (session_dir / "rollback_stack.json").write_text(json.dumps([entry]))
    init_session("session")
    history = load_query_history("session")
    assert history[0]["commit_hash"] == "abc12345"
    assert history[0]["rollbackable"] is True


def test_read_query_records_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "test.db"
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO items VALUES (1, 'one')")
        connection.commit()

    monkeypatch.setenv("DYNAMIC_DB_URI", f"sqlite:///{database}")
    monkeypatch.setenv("SESSION_NAME", "session")
    result = mcp_server.read_query("SELECT * FROM items")
    assert '"one"' in result
    history = load_query_history("session")
    assert history[-1]["operation"] == "SELECT"
    assert history[-1]["rows_affected"] == 1
    assert history[-1]["rollbackable"] is False
