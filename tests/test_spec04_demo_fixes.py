"""Tests for Spec 04 demo fixes: key rotation, pinned models, schema panel,
event-loop lifecycle, and the gate for undo/revert actions."""

import asyncio
import json
import os
import sqlite3

import pytest

from db_agent.demo_config import (
    DEMO_ORCHESTRATOR_MODEL,
    DEMO_WORKER_MODEL,
    _new_rotator,
    demo_api_keys,
    make_key_rotator,
)
from db_agent.gate import GateDecision


# ---------------------------------------------------------------------------
# Section 3 — key rotation
# ---------------------------------------------------------------------------


def test_round_robin_rotates_through_all_keys():
    rotator = _new_rotator(["k1", "k2", "k3"])
    assert [next(rotator) for _ in range(7)] == ["k1", "k2", "k3", "k1", "k2", "k3", "k1"]


def test_round_robin_single_key_repeats():
    rotator = _new_rotator(["k1"])
    assert [next(rotator) for _ in range(3)] == ["k1", "k1", "k1"]


def test_make_key_rotator_reads_demo_env(monkeypatch):
    monkeypatch.setenv("DEMO_GROQ_KEYS", "key1, key2 ,key3")
    rotator = make_key_rotator("groq")
    assert [next(rotator) for _ in range(3)] == ["key1", "key2", "key3"]


def test_demo_keys_falls_back_to_single_key_env(monkeypatch):
    monkeypatch.delenv("DEMO_GROQ_KEYS", raising=False)
    monkeypatch.setenv("groq_api_key", "single")
    assert demo_api_keys("groq") == ["single"]


def test_demo_keys_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv("DEMO_GROQ_KEYS", raising=False)
    monkeypatch.delenv("groq_api_key", raising=False)
    monkeypatch.delenv("DBAGENT_GROQ_KEY", raising=False)
    assert demo_api_keys("groq") == []


# ---------------------------------------------------------------------------
# Section 3 — pinned demo models
# ---------------------------------------------------------------------------


def test_demo_models_are_fixed_constants():
    assert isinstance(DEMO_ORCHESTRATOR_MODEL, str)
    assert isinstance(DEMO_WORKER_MODEL, str)
    assert DEMO_ORCHESTRATOR_MODEL
    assert DEMO_WORKER_MODEL
    # The worker must be tool-capable — a pinned constant, never resolved
    # from the live catalog at request time.
    assert DEMO_WORKER_MODEL == "llama-3.1-8b-instant"
    assert DEMO_ORCHESTRATOR_MODEL == "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# Section 1 — event-loop lifecycle
# ---------------------------------------------------------------------------


def test_web_has_exactly_one_asyncio_run_call_site():
    import ast

    import db_agent.web as web

    source = open(web.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                count += 1
    assert count == 1


# ---------------------------------------------------------------------------
# Section 5 — undo/revert go through the gate
# ---------------------------------------------------------------------------


class _FakeSessionState(dict):
    """Mimics Streamlit's session_state: attribute and item access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def test_undo_is_parked_requires_confirm(monkeypatch):
    import db_agent.web as web

    monkeypatch.setattr(web.st, "session_state", _FakeSessionState())
    web._set_pending("undo", {"commit_group_id": None})
    pending = web.st.session_state["pending_mutation"]
    assert pending["action"] == "undo"
    assert pending["operation"] == "UNDO"
    assert "mutation" in pending["reason"].lower()


def test_revert_is_parked_requires_confirm(monkeypatch):
    import db_agent.web as web

    monkeypatch.setattr(web.st, "session_state", _FakeSessionState())
    web._set_pending("revert", {"commit_hash": "abc12345"})
    pending = web.st.session_state["pending_mutation"]
    assert pending["action"] == "revert"
    assert pending["operation"] == "REVERT"
    assert "mutation" in pending["reason"].lower()


def test_gate_parks_risky_mutation_and_aborts(monkeypatch):
    import db_agent.web as web

    monkeypatch.setattr(web.st, "session_state", _FakeSessionState())
    decision = web._gate(
        "execute_smart_mutation",
        {
            "table_name": "users",
            "sql_query": "DELETE FROM users WHERE id = 1",
            "where_condition": "id = 1",
        },
    )
    assert decision.action == "deny_abort"
    assert web.st.session_state["pending_mutation"]["action"] == "mutation"
    assert web.st.session_state["pending_mutation"]["operation"] == "DELETE"


def test_gate_approves_safe_mutation(monkeypatch):
    import db_agent.web as web

    monkeypatch.setattr(web.st, "session_state", _FakeSessionState())
    decision = web._gate(
        "execute_smart_mutation",
        {
            "table_name": "users",
            "sql_query": "INSERT INTO users (id, name) VALUES (3, 'Carol')",
            "where_condition": "id = 3",
        },
    )
    assert decision.action == "approve"
    assert "pending_mutation" not in web.st.session_state


def test_gate_read_query_is_not_gated(monkeypatch):
    import db_agent.web as web

    monkeypatch.setattr(web.st, "session_state", _FakeSessionState())
    decision = web._gate("read_query", {"sql_query": "SELECT * FROM users"})
    assert decision.action == "approve"


# ---------------------------------------------------------------------------
# Section 4 — diff rendering helpers
# ---------------------------------------------------------------------------


def test_diff_update_changed_cells():
    from db_agent.web import _diff_rows

    diffs = _diff_rows(
        [{"id": 1, "name": "Alice", "role": "admin"}],
        [{"id": 1, "name": "Alice", "role": "customer"}],
        ["id"],
    )
    assert diffs[0]["change"] == "changed"
    assert diffs[0]["changed_cols"] == ["role"]
    assert diffs[0]["before"]["role"] == "admin"
    assert diffs[0]["after"]["role"] == "customer"


def test_diff_delete_marks_removed():
    from db_agent.web import _diff_rows

    diffs = _diff_rows([{"id": 2, "name": "Bob"}], [], ["id"])
    assert diffs[0]["change"] == "removed"
    assert diffs[0]["after"] is None


def test_diff_insert_marks_added():
    from db_agent.web import _diff_rows

    diffs = _diff_rows([], [{"id": 3, "name": "Carol"}], ["id"])
    assert diffs[0]["change"] == "added"
    assert diffs[0]["before"] is None


def test_diff_unchanged_rows_have_no_change():
    from db_agent.web import _diff_rows

    diffs = _diff_rows([{"id": 1, "name": "Alice"}], [{"id": 1, "name": "Alice"}], ["id"])
    assert diffs[0]["change"] == ""
    assert diffs[0]["changed_cols"] == []


def test_sql_literal_escapes_strings():
    from db_agent.web import _sql_literal

    assert _sql_literal("O'Brien") == "'O''Brien'"
    assert _sql_literal(42) == "42"
    assert _sql_literal(None) == "NULL"


# ---------------------------------------------------------------------------
# Section 1 — real multi-query lifecycle against a sandboxed SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    database = tmp_path / "demo.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES (1, 'Alice')")
        conn.execute("INSERT INTO users VALUES (2, 'Bob')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DYNAMIC_DB_URI", f"sqlite:///{database}")
    monkeypatch.setenv("SESSION_NAME", "spec04-test")
    return f"sqlite:///{database}"


def _mutation_ok(result: str) -> bool:
    try:
        payload = json.loads(result)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "success"


def test_consecutive_mutations_no_event_loop_error(demo_env):
    """3+ consecutive tracked mutations across separate asyncio.run() calls
    must not raise 'Event loop is closed' — the adapter is created and closed
    inside the same coroutine each time."""
    from db_agent import mcp_server

    for i in range(3):
        result = mcp_server.execute_smart_mutation(
            "users",
            f"INSERT INTO users (id, name) VALUES ({10 + i}, 'user{i}')",
            f"id = {10 + i}",
        )
        assert _mutation_ok(result), result


def test_undo_and_revert_restore_state(demo_env):
    """The web undo/revert path (via the same inverse-query logic the CLI uses)
    restores DB state and updates the rollback stack."""
    from db_agent import mcp_server
    from db_agent.tracker import load_rollback_stack, save_rollback_stack

    result = mcp_server.execute_smart_mutation(
        "users",
        "UPDATE users SET name = 'Alice2' WHERE id = 1",
        "id = 1",
    )
    assert _mutation_ok(result)

    stack = load_rollback_stack("spec04-test")
    entry = stack[-1]
    assert entry["operation"] == "UPDATE"
    assert entry["before"] == [{"id": 1, "name": "Alice"}]

    # Simulate the web undo path: pop the group and replay inverse queries.
    from sqlalchemy import create_engine, text

    from db_agent.tracker import generate_inverse_queries

    entries = [stack.pop()]
    engine = create_engine(demo_env)
    with engine.begin() as conn:
        for e in entries:
            for sql, params in generate_inverse_queries(e):
                conn.execute(text(sql), params)
    save_rollback_stack("spec04-test", stack)

    with sqlite3.connect(demo_env.replace("sqlite:///", "")) as conn:
        name = conn.execute("SELECT name FROM users WHERE id = 1").fetchone()[0]
    assert name == "Alice"
    assert load_rollback_stack("spec04-test") == []
