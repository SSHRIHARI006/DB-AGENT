"""Streamlit demo UI for db-agent (demo mode only).

Runs against a fresh sandboxed ``test.db`` populated by
``populate_db.populate_database``, with a limited number of tries per browser
session tracked in ``st.session_state``.

The human-in-the-loop gate is **structurally non-bypassable** here: there is
no auto-approve flag anywhere in this module, and the confirmation step is the
only code path that reaches ``execute_smart_mutation`` for risky operations.
Undo/revert actions are themselves mutations and go through the same gate.

Event-loop contract: exactly one ``asyncio.run()`` per user-triggered request.
All adapter work (model validation, query execution, adapter cleanup) happens
inside one coroutine so the httpx client is always closed on the loop that
created it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import streamlit as st

from db_agent.demo_config import (
    DemoEndpoint,
    demo_api_keys,
    make_key_rotator,
    next_demo_endpoint,
)
from db_agent.gate import GateDecision
from db_agent.guardrails import classify_risk, classify_statement
from db_agent.mcp_server import (
    execute_smart_mutation,
    get_schema,
    read_query,
)
from db_agent.tracker import (
    generate_inverse_queries,
    load_rollback_stack,
    save_rollback_stack,
)

MAX_TRIES = 10

DB_PATH = Path(tempfile.gettempdir()) / "db_agent_demo" / "demo.db"


def _reset_demo_db() -> None:
    """Recreate the sandboxed demo database from scratch."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    try:
        from populate_db import populate_database
    except ImportError:
        # Fallback for environments where the repo root is not on sys.path
        # (e.g. when db_agent is installed as a wheel without populate_db).
        import importlib.util
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("populate_db", root / "populate_db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        populate_database = module.populate_database

    populate_database(DB_PATH)


def _run_with_env(db_uri: str, session_name: str, func, *args):
    """Run an in-process mcp_server call against the given DB/session."""
    old_uri = os.environ.get("DYNAMIC_DB_URI")
    old_session = os.environ.get("SESSION_NAME")
    os.environ["DYNAMIC_DB_URI"] = db_uri
    os.environ["SESSION_NAME"] = session_name
    try:
        return func(*args)
    finally:
        if old_uri is None:
            os.environ.pop("DYNAMIC_DB_URI", None)
        else:
            os.environ["DYNAMIC_DB_URI"] = old_uri
        if old_session is None:
            os.environ.pop("SESSION_NAME", None)
        else:
            os.environ["SESSION_NAME"] = old_session


def _set_pending(action: str, payload: dict[str, Any]) -> None:
    """Park an action for explicit user confirmation (the non-bypassable gate)."""
    st.session_state.pending_mutation = {
        "action": action,
        "payload": payload,
        "operation": "UNDO" if action == "undo" else "REVERT",
        "table": payload.get("table", ""),
        "sql": payload.get("sql", ""),
        "reason": (
            "UNDO reverts the latest mutation group — this is itself a mutation "
            "that changes database state."
            if action == "undo"
            else "REVERT restores an earlier database checkpoint by replaying "
            "inverse operations — this is itself a mutation that changes "
            "database state."
        ),
    }


def _park_mutation(tool_name: str, args: dict) -> None:
    """Park a risky mutation for the confirmation UI (the non-bypassable gate)."""
    sql_query = str(args.get("sql_query", ""))
    table_name = str(args.get("table_name", ""))
    where_condition = str(args.get("where_condition", "") or "")
    operation = classify_statement(sql_query)
    risk = classify_risk(operation, where_condition, table=table_name)

    if not risk.risky:
        return

    st.session_state.pending_mutation = {
        "action": "mutation",
        "payload": args,
        "operation": operation,
        "table": table_name,
        "sql": sql_query,
        "reason": risk.reason,
    }


def _gate(tool_name: str, args: dict) -> GateDecision:
    """Streamlit-specific gate hook (sync, called from the worker's hook).

    Auto-approve does not exist here. A risky mutation is parked in
    ``st.session_state.pending_mutation`` and the worker is told to abort; the
    user must click **Approve and execute** on the next rerun, which is the
    only path that resumes it.
    """
    if tool_name != "execute_smart_mutation":
        return GateDecision.approve()
    _park_mutation(tool_name, args)
    if st.session_state.get("pending_mutation"):
        return GateDecision.deny_abort(
            "Risky mutation parked for manual approval — see the confirmation panel."
        )
    return GateDecision.approve()


async def _async_gate(tool_name: str, args: dict) -> GateDecision:
    """Async gate hook for the worker: parks risky mutations, aborts the task.

    The worker turns ``deny_abort`` into ``{"status": "aborted"}`` and returns
    cleanly, so the request coroutine unwinds (closing the adapter) instead of
    aborting via exception.
    """
    return _gate(tool_name, args)


def _render_pending_mutation() -> GateDecision | None:
    """Render the approval UI for a parked risky action, if any."""
    pending = st.session_state.get("pending_mutation")
    if not pending:
        return None
    action = pending.get("action", "mutation")
    st.warning(
        f"**Risky {pending['operation']} on `{pending['table']}`** — {pending['reason']}"
    )
    if pending.get("sql"):
        st.code(pending["sql"], language="sql")
    col1, col2 = st.columns(2)
    if col1.button("Approve and execute", key=f"approve_{action}"):
        st.session_state.pop("pending_mutation", None)
        return GateDecision.approve(approved_via="manual", message=pending["reason"])
    if col2.button("Block", key=f"block_{action}"):
        st.session_state.pop("pending_mutation", None)
        return GateDecision.deny_abort(f"Blocked by user: {pending['reason']}")
    st.info("This action will not run until you click **Approve and execute**.")
    return None


def _init_state() -> None:
    if "tries" not in st.session_state:
        st.session_state.tries = 0
    if "db_uri" not in st.session_state:
        _reset_demo_db()
        st.session_state.db_uri = f"sqlite:///{DB_PATH}"
        st.session_state.session_name = "web_demo"
        st.session_state.last_results = []
        st.session_state.last_diff = None
    if "key_rotators" not in st.session_state:
        # One rotator per provider, kept per browser session. Rotating a fresh
        # rotator on every click would defeat round-robin across queries.
        st.session_state.key_rotators = {}
    if "schema_text" not in st.session_state:
        # Static sandboxed schema: fetch once per session, not per query.
        st.session_state.schema_text = _run_with_env(
            st.session_state.db_uri,
            st.session_state.session_name,
            get_schema,
        )


def _tries_left() -> int:
    return MAX_TRIES - st.session_state.tries


def _render_schema_panel() -> None:
    st.sidebar.subheader("Database schema")
    st.sidebar.caption("SQLite — sandboxed demo database, resets periodically")
    st.sidebar.code(st.session_state.schema_text, language="text")


def _rollback_entries() -> list[dict[str, Any]]:
    return load_rollback_stack(st.session_state.session_name)


def _render_rollback_history() -> None:
    """List rollback-stack entries with Undo / Revert controls.

    Both actions are mutations: they are parked for confirmation like any
    other risky mutation, never executed silently.
    """
    entries = _rollback_entries()
    if not entries:
        st.caption("No mutations yet.")
        return
    for entry in reversed(entries):
        label = (
            f"`{entry['commit_hash'][:8]}` · {entry['operation']} · "
            f"{entry['table']} · {entry['timestamp'][:19]}"
        )
        cols = st.columns([3, 1])
        cols[0].markdown(label)
        cols[1].markdown(f"`{entry.get('query', '')[:60]}`")
        if st.button("Revert to here", key=f"revert_{entry['commit_hash']}"):
            _set_pending("revert", {"commit_hash": entry["commit_hash"]})
            st.rerun()
    if st.button("Undo last", key="undo_last"):
        _set_pending("undo", {"commit_group_id": None})
        st.rerun()


def _schema_table_has_pk(table_name: str) -> list[str]:
    """Return the primary-key columns of a table via SQLAlchemy reflection."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(st.session_state.db_uri)
    inspector = inspect(engine)
    return inspector.get_pk_constraint(table_name).get("constrained_columns") or []


def _fetch_after_rows(table_name: str, before_rows: list[dict], primary_keys: list[str]) -> list[dict]:
    """Fetch the post-mutation state for the same rows as ``before_rows``.

    Builds a WHERE clause from the before-row primary-key values (mirroring
    ``tracker.generate_inverse_queries``) and runs it through ``read_query``.
    Falls back to a table-wide scan if there is no usable primary key — the
    demo schema is tiny and sandboxed.
    """
    if not before_rows or not primary_keys:
        return []
    conditions = []
    for row in before_rows:
        cond = " AND ".join(
            f"{pk} = {_sql_literal(row.get(pk))}" for pk in primary_keys if pk in row
        )
        if cond:
            conditions.append(f"({cond})")
    if not conditions:
        return []
    where = " OR ".join(conditions)
    sql = f"SELECT * FROM {table_name} WHERE {where}"
    return _run_with_env(st.session_state.db_uri, st.session_state.session_name, read_query, sql)


def _diff_rows(before_rows: list[dict], after_rows: list[dict], primary_keys: list[str]) -> list[dict]:
    """Pair before/after rows and mark changed cells.

    Returns a list of per-row change descriptions: ``"changed"``, ``"added"``,
    ``"removed"`` or ``""`` (unchanged), with before and after values.
    """
    if before_rows is None:
        before_rows = []
    if after_rows is None:
        after_rows = []

    def key(row: dict) -> tuple:
        return tuple(str(row.get(pk)) for pk in primary_keys if pk in row)

    after_by_key = {key(row): row for row in after_rows}
    diffs = []
    for before in before_rows:
        after = after_by_key.get(key(before))
        if after is None:
            diffs.append({"change": "removed", "before": dict(before), "after": None})
            continue
        changed_cols = {
            col for col in set(before) | set(after) if before.get(col) != after.get(col)
        }
        diffs.append(
            {
                "change": "changed" if changed_cols else "",
                "before": dict(before),
                "after": dict(after),
                "changed_cols": sorted(changed_cols),
            }
        )
    for after in after_rows:
        if key(after) not in {key(b) for b in before_rows}:
            diffs.append({"change": "added", "before": None, "after": dict(after)})
    return diffs


def _render_diff(table_name: str, before_rows: list[dict], after_rows: list[dict], primary_keys: list[str]) -> None:
    st.subheader("DB change diff")
    st.caption(f"Before/after for `{table_name}`")
    diffs = _diff_rows(before_rows, after_rows, primary_keys)
    if not diffs:
        st.caption("No before rows captured — nothing to diff.")
        return
    for diff in diffs:
        change = diff["change"]
        if change == "changed":
            key_desc = ", ".join(f"{pk}={diff['before'].get(pk)}" for pk in primary_keys if pk in diff["before"])
            st.markdown(f"**Changed row** ({key_desc})")
            left, right = st.columns(2)
            with left:
                st.caption("Before")
                st.table(diff["before"])
            with right:
                st.caption("After")
                st.table(diff["after"])
        elif change == "removed":
            st.markdown("**Removed row**")
            st.table(diff["before"])
        elif change == "added":
            st.markdown("**Added row**")
            st.table(diff["after"])


def _extract_result_meta(result_text: str) -> dict[str, Any]:
    """Parse the mutation result JSON (if any) for table/rows/operation."""
    import json

    try:
        payload = json.loads(result_text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {}
    return payload


def _display_results(results: list[str], diffs: list[dict] | None) -> None:
    for line in results:
        st.code(line, language="json")
    if diffs:
        for diff in diffs:
            _render_diff(diff["table"], diff["before"], diff["after"], diff["primary_keys"])


def main() -> None:
    st.set_page_config(page_title="DB-Agent Demo", page_icon="🗄️", layout="wide")
    _init_state()

    st.title("🗄️ DB-Agent Demo")
    st.caption(
        "Sandboxed SQLite demo. Each browser session gets "
        f"**{MAX_TRIES} tries**. Risky mutations always ask for confirmation."
    )

    _render_schema_panel()

    with st.expander("Undo / Revert — rollback history"):
        _render_rollback_history()

    left, right = st.columns(2)
    left.metric("Tries remaining", max(0, _tries_left()))
    if st.button("Reset demo database", key="reset_db"):
        _reset_demo_db()
        st.session_state.db_uri = f"sqlite:///{DB_PATH}"
        st.session_state.tries = 0
        st.session_state.pop("pending_mutation", None)
        st.session_state.last_results = []
        st.session_state.last_diff = None
        st.session_state.schema_text = _run_with_env(
            st.session_state.db_uri,
            st.session_state.session_name,
            get_schema,
        )
        st.rerun()

    if _tries_left() <= 0:
        st.error("You have used all your tries. Click **Reset demo database** to start over.")
        st.stop()

    # If a risky action is parked, show the approval UI. Approving executes
    # the parked action directly (the LLM already produced it).
    if st.session_state.get("pending_mutation"):
        decision = _render_pending_mutation()
        if decision is None:
            st.stop()
        if decision.action == "deny_abort":
            st.error(decision.message)
            st.stop()
        # Approve path: execute the parked action now.
        pending = st.session_state.pop("pending_mutation", None)
        _execute_parked_action(pending)
        st.stop()

    if st.session_state.get("last_results"):
        st.subheader("Last result")
        _display_results(st.session_state.last_results, st.session_state.last_diff)

    st.subheader("Ask a question in natural language")
    user_query = st.text_input("Query", placeholder="e.g. Show all users with role 'customer'")

    if not user_query.strip():
        st.info("Type a request and press Enter.")
        st.stop()

    if st.button("Run", key="run_query"):
        st.session_state.tries += 1
        st.session_state.last_query = user_query.strip()
        _execute_request(user_query.strip())


def _execute_parked_action(pending: dict) -> None:
    """Execute an action the user just approved in the confirmation UI."""
    action = pending.get("action", "mutation")
    if action in ("undo", "revert"):
        _perform_undo_revert(action, pending["payload"])
        return
    _execute_parked_mutation(pending)


def _perform_undo_revert(action: str, payload: dict) -> None:
    """Run undo_last_group / revert_to_hash and record the change for display.

    Both go through the confirmation gate (parked in ``_set_pending``). The
    undo/revert itself is executed directly against the engine — it is not a
    tool call, so it cannot be intercepted by the worker gate — but the user
    has already explicitly confirmed it.
    """
    db_uri = st.session_state.db_uri
    session_name = st.session_state.session_name
    stack = load_rollback_stack(session_name)
    if not stack:
        st.error("Rollback stack is empty — nothing to undo or revert.")
        return

    entries_affected: list[dict] = []
    if action == "undo":
        last_entry = stack[-1]
        group_id = last_entry.get("commit_group_id", last_entry.get("commit_hash"))
        entries_affected = []
        while stack and stack[-1].get("commit_group_id", stack[-1].get("commit_hash")) == group_id:
            entries_affected.append(stack.pop())
    elif action == "revert":
        target_hash = payload.get("commit_hash")
        target_idx = -1
        for i, entry in enumerate(stack):
            if entry.get("commit_hash") == target_hash:
                target_idx = i
                break
        if target_idx == -1:
            st.error(f"Commit `{target_hash}` not found in the rollback stack.")
            return
        entries_affected = stack[target_idx + 1:]
        if not entries_affected:
            st.info("Database is already at that commit — nothing to do.")
            return
        stack = stack[: target_idx + 1]

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(db_uri)
        with engine.begin() as conn:
            for entry in reversed(entries_affected):
                for sql, params in generate_inverse_queries(entry):
                    conn.execute(text(sql), params)
        save_rollback_stack(session_name, stack)

        affected = ", ".join(
            f"`{entry['commit_hash'][:8]}` ({entry['operation']} on {entry['table']})"
            for entry in reversed(entries_affected)
        )
        st.success(f"Reverted {len(entries_affected)} operation(s): {affected}")

        # Refresh displayed state and the diff/result area.
        st.session_state.schema_text = _run_with_env(db_uri, session_name, get_schema)
        # After an undo/revert the last diff is stale; clear it so the next
        # mutation renders a fresh before/after.
        st.session_state.last_diff = None
    except Exception as exc:  # noqa: BLE001 - demo surface, show the error
        st.error(f"Undo/revert failed: {exc}")
        # Restore the stack on failure so entries are not lost.
        save_rollback_stack(session_name, stack + entries_affected)


def _execute_parked_mutation(pending: dict) -> None:
    """Execute a mutation the user just approved in the confirmation UI."""
    session_name = st.session_state.session_name
    db_uri = st.session_state.db_uri
    args = pending["payload"]
    result = _run_with_env(
        db_uri,
        session_name,
        execute_smart_mutation,
        args["table_name"],
        args["sql_query"],
        args.get("where_condition", ""),
        args.get("commit_group_id"),
        "manual",
    )
    st.code(result, language="json")
    st.success("Mutation executed after manual approval.")
    st.session_state.last_results = [result]
    st.session_state.last_diff = None


def _execute_request(user_query: str) -> None:
    """Plan and execute the request using the in-process worker/orchestrator.

    Uses the real provider stack (adapter + worker). The gate hook is the
    Streamlit one above — there is no auto-approve path. Exactly one
    ``asyncio.run()`` per request, wrapping the whole adapter lifecycle so the
    httpx client is created and closed on the same loop.
    """

    def _resolve_provider() -> tuple[str, str, str, str]:
        """Return ``(provider, api_key, orchestrator_model, worker_model)``.

        Demo mode rotates across a pool of (provider, model) endpoints so no
        single provider's key/quota gets exhausted. If the pool is empty
        (e.g. no provider configured), fall back to any single provider that
        has a key and use its first static model for both roles.
        """
        from db_agent.provider_config import load_provider_config

        session_name = st.session_state.session_name
        endpoint = next_demo_endpoint()
        if endpoint is not None:
            rotator = st.session_state.key_rotators.get(endpoint.provider)
            if rotator is None:
                rotator = make_key_rotator(endpoint.provider)
                st.session_state.key_rotators[endpoint.provider] = rotator
            api_key = next(rotator)
            return endpoint.provider, api_key, endpoint.orchestrator_model, endpoint.worker_model

        # No endpoint pool: fall back to the first provider with a key.
        config = load_provider_config(session_name)
        provider = None
        for candidate in ("groq", "gemini", "openrouter", "ollama_cloud", "nvidia_nim"):
            if config.active_provider == candidate and candidate in config.providers:
                resolved = resolve_api_key(session_name, candidate, config.providers[candidate])
                if resolved:
                    provider, _ = candidate, resolved
                    break
            keys = demo_api_keys(candidate)
            if keys:
                provider = candidate
                break
        if not provider:
            raise RuntimeError(
                "No provider API key found. Set one in the server's .env "
                "(e.g. groq_api_key=...)."
            )
        rotator = st.session_state.key_rotators.get(provider)
        if rotator is None:
            rotator = make_key_rotator(provider)
            st.session_state.key_rotators[provider] = rotator
        api_key = next(rotator)
        from db_agent.providers.static_models import STATIC_MODELS

        models = STATIC_MODELS.get(provider, [])
        model = models[0].id if models else "gpt-4o-mini"
        return provider, api_key, model, model

    async def _run() -> list[dict[str, Any]]:
        from db_agent.agents.orchestrator import plan_dag
        from db_agent.agents.worker import execute_task
        from db_agent.providers import create_adapter

        session_name = st.session_state.session_name
        db_uri = st.session_state.db_uri
        provider, api_key, orchestrator_model, worker_model = _resolve_provider()
        adapter = create_adapter(provider, api_key)
        try:
            # Demo mode: models come from the rotating endpoint pool — no
            # /models list call per request.
            schema_text = _run_with_env(db_uri, session_name, get_schema)
            dag = await plan_dag(user_query, schema_text, adapter, orchestrator_model)
            if not dag:
                return [{"result": "Failed to generate an execution plan.", "status": "error"}]

            import secrets

            group_id = secrets.token_hex(4)
            results: list[dict[str, Any]] = []
            for task in dag:
                result = await execute_task(
                    task,
                    schema_text,
                    group_id,
                    _InProcessSession(db_uri, session_name),
                    adapter,
                    worker_model,
                    pre_execute_hook=_async_gate,
                )
                results.append({"result": result["result"], "status": result["status"]})
            return results
        finally:
            await adapter.close()

    try:
        results = asyncio.run(_run())
        # A risky mutation was parked: the worker returned aborted results and
        # the confirmation UI is showing. Don't display the aborted task as a
        # result or build a diff for it.
        if st.session_state.get("pending_mutation"):
            return
        display_lines: list[str] = []
        diffs: list[dict[str, Any]] = []
        for result in results:
            display_lines.append(result["result"])
            meta = _extract_result_meta(result["result"])
            if meta and meta.get("operation") in ("INSERT", "UPDATE", "DELETE"):
                table = meta.get("table", "")
                primary_keys = _schema_table_has_pk(table)
                before_rows = _load_before_rows(table)
                if primary_keys and before_rows:
                    diffs.append(
                        {
                            "table": table,
                            "before": before_rows,
                            "after": _fetch_after_rows(table, before_rows, primary_keys),
                            "primary_keys": primary_keys,
                        }
                    )
        st.session_state.last_results = display_lines
        st.session_state.last_diff = diffs if diffs else None
        _display_results(display_lines, st.session_state.last_diff)
    except st.runtime.scriptrunner.script_runner.StopException:
        # Belt-and-braces: if any Streamlit stop is triggered inside the
        # request path, treat it as a parked mutation, not an error.
        pass
    except Exception as exc:  # noqa: BLE001 - demo surface, show the error
        st.error(f"Execution failed: {exc}")


def _load_before_rows(table_name: str) -> list[dict]:
    """Reconstruct the before-state of the latest rollback entry for a table."""
    stack = _rollback_entries()
    for entry in reversed(stack):
        if entry.get("table") == table_name and entry.get("operation") in ("INSERT", "UPDATE", "DELETE"):
            return entry.get("before", [])
    return []


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class _InProcessSession:
    """Minimal stand-in for the MCP client session used by the worker.

    The web path calls the mcp_server functions directly (no subprocess), so
    the worker only needs ``list_tools`` and ``call_tool`` — implemented here
    against the real functions.
    """

    _TOOLS = [
        {
            "name": "read_query",
            "description": "Execute a read-only SQL query and return rows as JSON.",
            "parameters": {"type": "object", "properties": {"sql_query": {"type": "string"}}, "required": ["sql_query"]},
        },
        {
            "name": "execute_smart_mutation",
            "description": "Execute a tracked INSERT/UPDATE/DELETE mutation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "sql_query": {"type": "string"},
                    "where_condition": {"type": "string"},
                    "commit_group_id": {"type": ["string", "null"]},
                    "approved_via": {"type": "string"},
                },
                "required": ["table_name", "sql_query", "where_condition"],
            },
        },
    ]

    def __init__(self, db_uri: str, session_name: str):
        self._db_uri = db_uri
        self._session_name = session_name

    async def list_tools(self):
        from types import SimpleNamespace

        tools = []
        for tool in self._TOOLS:
            tools.append(SimpleNamespace(name=tool["name"], description=tool["description"], inputSchema=tool["parameters"]))
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name: str, args: dict):
        from types import SimpleNamespace

        if name == "read_query":
            text = _run_with_env(self._db_uri, self._session_name, read_query, args["sql_query"])
        elif name == "execute_smart_mutation":
            text = _run_with_env(
                self._db_uri,
                self._session_name,
                execute_smart_mutation,
                args["table_name"],
                args["sql_query"],
                args.get("where_condition", ""),
                args.get("commit_group_id"),
                args.get("approved_via", "manual"),
            )
        else:
            raise ValueError(f"Unknown tool: {name}")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


if __name__ == "__main__":
    main()
