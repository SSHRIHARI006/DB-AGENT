from types import SimpleNamespace

import pytest

from db_agent.agents.worker import execute_task
from db_agent.providers.base import GenerationResult, ToolCall


class CapturingAdapter:
    def __init__(self):
        self.tools = None

    async def generate(self, *, messages, model, tools, temperature):
        self.tools = tools
        return GenerationResult(
            tool_calls=[
                ToolCall(
                    "execute_smart_mutation",
                    {
                        "table_name": "products",
                        "sql_query": "INSERT INTO products VALUES (4, 'gamepad', 1000, 5)",
                        "where_condition": "id = 4",
                    },
                )
            ]
        )


class FakeMcpSession:
    def __init__(self):
        self.called_args = None

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="execute_smart_mutation",
                    description="Execute a tracked mutation",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string"},
                            "sql_query": {"type": "string"},
                            "where_condition": {"type": "string"},
                            "commit_group_id": {"type": ["string", "null"]},
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
async def test_internal_commit_group_id_is_hidden_from_provider_schema():
    adapter = CapturingAdapter()
    mcp_session = FakeMcpSession()
    result = await execute_task(
        {"intent": "insert a product"},
        "Table: products",
        "group-123",
        mcp_session,
        adapter,
        "worker-model",
    )

    assert result["status"] == "success"
    schema = adapter.tools[0]["parameters"]
    assert "commit_group_id" not in schema["properties"]
    assert "commit_group_id" not in schema["required"]
    assert mcp_session.called_args[1]["commit_group_id"] == "group-123"
