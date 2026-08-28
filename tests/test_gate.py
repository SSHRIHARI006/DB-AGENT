from types import SimpleNamespace

import pytest

from db_agent.gate import GateDecision
from db_agent.agents.worker import execute_task
from db_agent.providers.base import GenerationResult, ToolCall


def test_gate_decision_defaults():
    decision = GateDecision(action="approve")
    assert decision.approved_via == "manual"
    assert decision.message == ""


def test_gate_decision_factories():
    assert GateDecision.approve().action == "approve"
    assert GateDecision.approve(approved_via="auto_flag").approved_via == "auto_flag"
    assert GateDecision.deny_retry("bad").action == "deny_retry"
    assert GateDecision.deny_abort("stop").action == "deny_abort"


class _MutationAdapter:
    def __init__(self):
        self.tools = None

    async def generate(self, *, messages, model, tools, temperature):
        self.tools = tools
        return GenerationResult(
            tool_calls=[
                ToolCall(
                    "execute_smart_mutation",
                    {
                        "table_name": "users",
                        "sql_query": "DELETE FROM users WHERE id = 1",
                        "where_condition": "id = 1",
                    },
                )
            ]
        )


class _FakeMcp:
    def __init__(self):
        self.called_args = None

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="execute_smart_mutation",
                    description="mutation",
                    inputSchema={
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
                )
            ]
        )

    async def call_tool(self, name, args):
        self.called_args = (name, args)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"status": "success"}')])


@pytest.mark.asyncio
async def test_approve_path_stamps_approved_via():
    adapter = _MutationAdapter()
    mcp = _FakeMcp()

    async def hook(name, args):
        assert name == "execute_smart_mutation"
        return GateDecision.approve(approved_via="manual")

    result = await execute_task(
        {"id": 1, "intent": "delete a user"},
        "Table: users",
        "group-1",
        mcp,
        adapter,
        "worker-model",
        pre_execute_hook=hook,
    )
    assert result["status"] == "success"
    assert mcp.called_args[1]["approved_via"] == "manual"
    assert mcp.called_args[1]["commit_group_id"] == "group-1"


@pytest.mark.asyncio
async def test_deny_abort_stops_execution():
    adapter = _MutationAdapter()
    mcp = _FakeMcp()

    async def hook(name, args):
        return GateDecision.deny_abort("User said no")

    result = await execute_task(
        {"id": 1, "intent": "delete a user"},
        "Table: users",
        "group-1",
        mcp,
        adapter,
        "worker-model",
        pre_execute_hook=hook,
    )
    assert result["status"] == "aborted"
    assert "User said no" in result["result"]
    assert mcp.called_args is None  # tool never executed


@pytest.mark.asyncio
async def test_deny_retry_triggers_retry():
    class DenyThenApproveAdapter(_MutationAdapter):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def generate(self, *, messages, model, tools, temperature):
            self.attempts += 1
            return GenerationResult(
                tool_calls=[
                    ToolCall(
                        "execute_smart_mutation",
                        {
                            "table_name": "users",
                            "sql_query": "DELETE FROM users WHERE id = 1",
                            "where_condition": "id = 1",
                        },
                    )
                ]
            )

    adapter = DenyThenApproveAdapter()
    mcp = _FakeMcp()
    calls = []

    async def hook(name, args):
        calls.append(name)
        if len(calls) == 1:
            return GateDecision.deny_retry("Try a safer query")
        return GateDecision.approve(approved_via="manual")

    result = await execute_task(
        {"id": 1, "intent": "delete a user"},
        "Table: users",
        "group-1",
        mcp,
        adapter,
        "worker-model",
        pre_execute_hook=hook,
    )
    assert result["status"] == "success"
    assert adapter.attempts == 2
    assert mcp.called_args is not None


@pytest.mark.asyncio
async def test_no_hook_means_no_gate_stamp():
    adapter = _MutationAdapter()
    mcp = _FakeMcp()
    result = await execute_task(
        {"id": 1, "intent": "delete a user"},
        "Table: users",
        "group-1",
        mcp,
        adapter,
        "worker-model",
    )
    assert result["status"] == "success"
    assert "approved_via" not in mcp.called_args[1]


def test_cli_gate_auto_approve_is_stamped(monkeypatch):
    from db_agent import cli

    monkeypatch.setattr(cli, "_AUTO_APPROVE", True)
    import asyncio

    decision = asyncio.run(
        cli._cli_gate(
            "execute_smart_mutation",
            {
                "table_name": "users",
                "sql_query": "DELETE FROM users",
                "where_condition": "",
            },
        )
    )
    assert decision.action == "approve"
    assert decision.approved_via == "auto_flag"


def test_cli_gate_read_query_is_not_gated():
    from db_agent import cli

    import asyncio

    decision = asyncio.run(
        cli._cli_gate("read_query", {"sql_query": "SELECT * FROM users"})
    )
    assert decision.action == "approve"
    assert decision.approved_via == "manual"


def test_cli_gate_safe_mutation_is_not_risky(monkeypatch):
    from db_agent import cli

    monkeypatch.setattr(cli, "_AUTO_APPROVE", False)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    import asyncio

    decision = asyncio.run(
        cli._cli_gate(
            "execute_smart_mutation",
            {
                "table_name": "users",
                "sql_query": "INSERT INTO users (id) VALUES (1)",
                "where_condition": "id = 1",
            },
        )
    )
    assert decision.action == "approve"
    assert decision.approved_via == "manual"
