import json
import sqlite3

import pytest

from db_agent import mcp_server
from db_agent.tracker import (
    add_rollback_entry,
    build_rollback_entry,
    load_rollback_stack,
    persist_rollback_entry,
)


@pytest.fixture
def demo_db(tmp_path):
    database = tmp_path / "demo.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES (1, 'Alice')")
        conn.execute("INSERT INTO users VALUES (2, 'Bob')")
    return f"sqlite:///{database}"


@pytest.fixture
def env(monkeypatch, demo_db, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DYNAMIC_DB_URI", demo_db)
    monkeypatch.setenv("SESSION_NAME", "test-session")


def test_insert_with_empty_where_is_rejected(env):
    result = mcp_server.execute_smart_mutation(
        "users",
        "INSERT INTO users (id, name) VALUES (3, 'Carol')",
        "",
    )
    assert "Error" in result
    assert "where_condition" in result
    # No rollback entry should exist for the rejected mutation
    assert load_rollback_stack("test-session") == []


def test_insert_with_where_records_rollback(env):
    result = mcp_server.execute_smart_mutation(
        "users",
        "INSERT INTO users (id, name) VALUES (3, 'Carol')",
        "id = 3",
    )
    assert '"status": "success"' in result
    stack = load_rollback_stack("test-session")
    assert len(stack) == 1
    assert stack[0]["operation"] == "INSERT"
    assert stack[0]["before"] == [{"id": 3, "name": "Carol"}]


def test_ddl_is_blocked(env):
    result = mcp_server.execute_smart_mutation(
        "users",
        "DROP TABLE users",
        "",
    )
    assert "DDL" in result
    assert load_rollback_stack("test-session") == []


def test_unknown_table_is_rejected(env):
    result = mcp_server.execute_smart_mutation(
        "missing",
        "INSERT INTO missing (id) VALUES (1)",
        "id = 1",
    )
    assert "does not exist" in result


def test_where_injection_is_rejected(env):
    result = mcp_server.execute_smart_mutation(
        "users",
        "DELETE FROM users WHERE id = 1",
        "id = 1; DROP TABLE users",
    )
    assert "Error" in result
    assert load_rollback_stack("test-session") == []


def test_approved_via_is_persisted(env):
    result = mcp_server.execute_smart_mutation(
        "users",
        "UPDATE users SET name = 'X' WHERE id = 1",
        "id = 1",
        approved_via="auto_flag",
    )
    assert '"status": "success"' in result
    stack = load_rollback_stack("test-session")
    assert stack[0]["approved_via"] == "auto_flag"

    import os

    session_dir = os.path.join(".db_agent", "sessions", "test-session")
    history = json.loads(open(os.path.join(session_dir, "query_history.json"), encoding="utf-8").read())
    assert history[-1]["approved_via"] == "auto_flag"


def test_datetime_value_round_trips_through_tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import datetime

    entry = build_rollback_entry(
        operation="INSERT",
        table="events",
        query="INSERT INTO events (id, happened_at) VALUES (1, '2026-01-01')",
        before=[{"id": 1, "happened_at": datetime.datetime(2026, 1, 1, 12, 0, 0)}],
        primary_keys=["id"],
    )
    commit_hash = persist_rollback_entry("session", entry)
    stack = load_rollback_stack("session")
    assert stack[0]["commit_hash"] == commit_hash
    # The datetime was serialized via default=str and loads back as a string
    assert stack[0]["before"][0]["happened_at"].startswith("2026-01-01")


def test_rollback_entry_uses_default_str_for_decimal(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from decimal import Decimal

    entry = build_rollback_entry(
        operation="UPDATE",
        table="products",
        query="UPDATE products SET price = 9.99 WHERE id = 1",
        before=[{"id": 1, "price": Decimal("9.99")}],
        primary_keys=["id"],
    )
    persist_rollback_entry("session", entry)
    stack = load_rollback_stack("session")
    assert stack[0]["before"][0]["price"] == "9.99"


def test_read_query_rejects_mutation_keywords(env):
    result = mcp_server.read_query("SELECT * FROM users; DELETE FROM users")
    assert "Error" in result


def test_json_write_failure_rolls_back_db_transaction(monkeypatch, env):
    """Simulate a rollback-log write failure: the DB mutation must not commit."""
    import os

    from db_agent import tracker

    session_dir = os.path.join(".db_agent", "sessions", "test-session")
    os.makedirs(session_dir, exist_ok=True)
    stack_path = os.path.join(session_dir, "rollback_stack.json")
    # Pre-create the stack file and make it read-only so the JSON write fails.
    with open(stack_path, "w", encoding="utf-8") as f:
        json.dump([], f)
    os.chmod(stack_path, 0o444)

    try:
        result = mcp_server.execute_smart_mutation(
            "users",
            "INSERT INTO users (id, name) VALUES (3, 'Carol')",
            "id = 3",
        )
        # The mutation should have failed because tracking could not be persisted.
        assert '"status": "error"' in result
    finally:
        os.chmod(stack_path, 0o644)

    # The DB must NOT contain the row — the transaction was rolled back.
    import sqlite3

    db_uri = os.environ["DYNAMIC_DB_URI"]
    db_path = db_uri.replace("sqlite:///", "")
    with sqlite3.connect(db_path) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM users")]
    assert "Carol" not in names
