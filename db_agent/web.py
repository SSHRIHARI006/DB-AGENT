"""Streamlit demo UI for db-agent (demo mode only).

Runs against a fresh sandboxed ``test.db`` populated by
``populate_db.populate_database``, with a limited number of tries per browser
session tracked in ``st.session_state``.

The human-in-the-loop gate is **structurally non-bypassable** here: there is
no auto-approve flag anywhere in this module, and the confirmation step is the
only code path that reaches ``execute_smart_mutation`` for risky operations.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from db_agent.gate import GateDecision
from db_agent.guardrails import classify_risk, classify_statement
from db_agent.mcp_server import (
    execute_smart_mutation,
    get_schema,
    read_query,
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


async def _gate(tool_name: str, args: dict) -> GateDecision:
    """Streamlit-specific gate hook (async to match the worker's protocol).

    Auto-approve does not exist here. A risky mutation is parked in
    ``st.session_state.pending_mutation`` and execution stops; the user must
    click **Approve and execute** on the next rerun, which is the only path
    that resumes it.
    """
    if tool_name != "execute_smart_mutation":
        return GateDecision.approve()

    sql_query = str(args.get("sql_query", ""))
    table_name = str(args.get("table_name", ""))
    where_condition = str(args.get("where_condition", "") or "")
    operation = classify_statement(sql_query)
    risk = classify_risk(operation, where_condition, table=table_name)

    if not risk.risky:
        return GateDecision.approve()

    # Park the risky mutation and show the confirmation UI.
    st.session_state.pending_mutation = {
        "tool": tool_name,
        "args": args,
        "operation": operation,
        "table": table_name,
        "sql": sql_query,
        "reason": risk.reason,
    }
    st.stop()


def _render_pending_mutation() -> GateDecision | None:
    """Render the approval UI for a parked risky mutation, if any."""
    pending = st.session_state.get("pending_mutation")
    if not pending:
        return None
    st.warning(
        f"**Risky {pending['operation']} on `{pending['table']}`** — {pending['reason']}"
    )
    st.code(pending["sql"], language="sql")
    col1, col2 = st.columns(2)
    if col1.button("Approve and execute", key="approve_mutation"):
        st.session_state.pop("pending_mutation", None)
        return GateDecision.approve(approved_via="manual", message=pending["reason"])
    if col2.button("Block", key="block_mutation"):
        st.session_state.pop("pending_mutation", None)
        return GateDecision.deny_abort(f"Blocked by user: {pending['reason']}")
    st.info("This risky mutation will not run until you click **Approve and execute**.")
    return None


def _init_state() -> None:
    if "tries" not in st.session_state:
        st.session_state.tries = 0
    if "db_uri" not in st.session_state:
        _reset_demo_db()
        st.session_state.db_uri = f"sqlite:///{DB_PATH}"
        st.session_state.session_name = "web_demo"


def _tries_left() -> int:
    return MAX_TRIES - st.session_state.tries


def main() -> None:
    st.set_page_config(page_title="DB-Agent Demo", page_icon="🗄️", layout="centered")
    _init_state()

    st.title("🗄️ DB-Agent Demo")
    st.caption(
        "Sandboxed SQLite demo. Each browser session gets "
        f"**{MAX_TRIES} tries**. Risky mutations always ask for confirmation."
    )

    left, right = st.columns(2)
    left.metric("Tries remaining", max(0, _tries_left()))
    if st.button("Reset demo database", key="reset_db"):
        _reset_demo_db()
        st.session_state.db_uri = f"sqlite:///{DB_PATH}"
        st.session_state.tries = 0
        st.session_state.pop("pending_mutation", None)
        st.rerun()

    if _tries_left() <= 0:
        st.error("You have used all your tries. Click **Reset demo database** to start over.")
        st.stop()

    # If a risky mutation is parked, show the approval UI. Approving executes
    # the parked mutation directly (the LLM already produced it).
    if st.session_state.get("pending_mutation"):
        decision = _render_pending_mutation()
        if decision is None:
            st.stop()
        if decision.action == "deny_abort":
            st.error(decision.message)
            st.stop()
        # Approve path: execute the parked mutation now.
        pending = st.session_state.pop("pending_mutation", None)
        _execute_parked_mutation(pending)
        st.stop()

    st.subheader("Ask a question in natural language")
    user_query = st.text_input("Query", placeholder="e.g. Show all users with role 'customer'")

    if not user_query.strip():
        st.info("Type a request and press Enter.")
        st.stop()

    if st.button("Run", key="run_query"):
        st.session_state.tries += 1
        st.session_state.last_query = user_query.strip()
        _execute_request(user_query.strip())


def _execute_parked_mutation(pending: dict) -> None:
    """Execute a mutation the user just approved in the confirmation UI."""
    session_name = st.session_state.session_name
    db_uri = st.session_state.db_uri
    args = pending["args"]
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


def _execute_request(user_query: str) -> None:
    """Plan and execute the request using the in-process worker/orchestrator.

    Uses the real provider stack (provider_config + model_cache + worker). The
    gate hook is the Streamlit one above — there is no auto-approve path.
    """
    import asyncio

    from db_agent.agents.orchestrator import plan_dag
    from db_agent.agents.worker import execute_task
    from db_agent.model_cache import get_models
    from db_agent.provider_config import (
        ProviderConfig,
        load_provider_config,
        resolve_api_key,
        selected_models,
    )
    from db_agent.providers import create_adapter, supported_providers

    session_name = st.session_state.session_name
    db_uri = st.session_state.db_uri

    # Demo mode has no interactive provider setup (that was cut with local
    # mode). Resolve the provider purely from environment keys, in priority
    # order, so a `.env` on the VPS is all that's needed.
    config: ProviderConfig = load_provider_config(session_name)
    provider = None
    entry = None
    api_key = None
    for candidate in supported_providers():
        if config.active_provider == candidate and candidate in config.providers:
            resolved = resolve_api_key(session_name, candidate, config.providers[candidate])
            if resolved:
                provider, entry, api_key = candidate, config.providers[candidate], resolved
                break
        from db_agent.provider_config import env_value_for

        env_cfg = env_value_for(candidate)
        if env_cfg:
            env_name, env_key = env_cfg
            provider, entry, api_key = candidate, None, env_key
            break

    if not provider or not api_key:
        st.error("No provider API key found. Set one in the server's .env (e.g. groq_api_key=...).")
        return

    adapter = create_adapter(provider, api_key, entry.base_url if entry else None)

    # Pick models: prefer the session's selected ones, else the first
    # orchestrator/worker-capable model from the catalog.
    orchestrator_model, worker_model = selected_models(config, provider) if entry else (None, None)
    if not orchestrator_model or not worker_model:
        try:
            from db_agent.model_cache import get_models

            async def _pick_models() -> tuple[str | None, str | None]:
                result = await get_models(session_name, provider, adapter)
                orch = next(
                    (m.id for m in result.models if m.supports_chat is not False),
                    None,
                )
                worker = next(
                    (m.id for m in result.models if m.supports_tools is not False),
                    None,
                )
                return orch, worker

            import asyncio

            orchestrator_model, worker_model = asyncio.run(_pick_models())
        except Exception as exc:  # noqa: BLE001 - demo surface
            st.error(f"Could not list models for {provider}: {exc}")
            return

    if not orchestrator_model or not worker_model:
        st.error(f"No usable models found for {provider}.")
        return

    async def _run() -> list[str]:
        from db_agent.agents.orchestrator import plan_dag as _plan_dag

        schema_text = _run_with_env(db_uri, session_name, get_schema)
        dag = await _plan_dag(user_query, schema_text, adapter, orchestrator_model)
        if not dag:
            return ["Failed to generate an execution plan."]

        import secrets

        group_id = secrets.token_hex(4)
        results = []
        for task in dag:
            result = await execute_task(
                task,
                schema_text,
                group_id,
                _InProcessSession(db_uri, session_name),
                adapter,
                worker_model,
                pre_execute_hook=_gate,
            )
            results.append(f"Task {task.get('id')}: {result['result']}")
        return results

    try:
        results = asyncio.run(_run())
        for line in results:
            st.code(line, language="json")
    except st.runtime.scriptrunner.script_runner.StopException:
        # A risky mutation was parked; the approval UI is showing. Not an error.
        pass
    except Exception as exc:  # noqa: BLE001 - demo surface, show the error
        st.error(f"Execution failed: {exc}")
    finally:
        asyncio.run(adapter.close())


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
