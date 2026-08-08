import copy
import json
import re
from typing import Any

from db_agent.providers import ProviderAdapter, ToolCall


def parse_fallback_tool_calls(content: str) -> list[dict[str, Any]]:
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
        matches = re.finditer(
            r"\{\s*\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"arguments\"\s*:\s*\{.*?\}\s*\}",
            content,
            re.DOTALL,
        )
        for match in matches:
            try:
                data = json.loads(match.group(0))
                tool_calls.append(data)
            except Exception:
                pass
    return tool_calls


def _normalized_calls(result, valid_tool_names: set[str]) -> list[dict[str, Any]]:
    calls = []
    for call in result.tool_calls or []:
        if isinstance(call, ToolCall):
            name, arguments = call.name, call.arguments
        else:
            name, arguments = call.get("name"), call.get("arguments")
        if name in valid_tool_names and isinstance(arguments, dict):
            calls.append({"name": name, "arguments": arguments})
    if not calls:
        calls = [
            call
            for call in parse_fallback_tool_calls(result.text)
            if call.get("name") in valid_tool_names and isinstance(call.get("arguments"), dict)
        ]
    return calls


async def execute_task(
    task: dict,
    schema_text: str,
    group_id: str,
    mcp_session,
    adapter: ProviderAdapter,
    model: str,
    max_retries: int = 3,
) -> dict[str, str]:
    intent = task.get("intent", "")
    mcp_tools = await mcp_session.list_tools()
    provider_tools = []
    valid_tool_names = set()
    for tool in mcp_tools.tools:
        valid_tool_names.add(tool.name)
        parameters = copy.deepcopy(tool.inputSchema)
        if tool.name == "execute_smart_mutation":
            properties = parameters.get("properties")
            if isinstance(properties, dict):
                properties.pop("commit_group_id", None)
            required = parameters.get("required")
            if isinstance(required, list):
                parameters["required"] = [name for name in required if name != "commit_group_id"]
        provider_tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": parameters,
            }
        )

    error_context = ""
    messages = [
        {
            "role": "system",
            "content": (
                "You are an atomic SQL execution worker. You translate a specific intent into exactly ONE database tool call.\n"
                "CRITICAL RULES:\n"
                "1. Read-only queries must use `read_query`.\n"
                "2. Mutations (INSERT, UPDATE, DELETE) must use `execute_smart_mutation`.\n"
                "3. Structural database modifications (DDL) are blocked.\n"
                f"CURRENT DATABASE SCHEMA:\n{schema_text}"
            ),
        }
    ]

    for _ in range(max_retries):
        current_msg = f"Intent: {intent}\n"
        if error_context:
            current_msg += f"{error_context}\nFix the SQL and try again."
        temp_messages = messages + [{"role": "user", "content": current_msg}]

        try:
            result = await adapter.generate(
                messages=temp_messages,
                model=model,
                tools=provider_tools,
                temperature=0.1,
            )
        except Exception as exc:
            return {"status": "error", "result": f"LLM Connection Error: {exc}"}

        calls = _normalized_calls(result, valid_tool_names)
        if not calls:
            error_context = "Error: You did not output a valid tool call. You MUST call a tool like execute_smart_mutation or read_query."
            continue

        call = calls[0]
        args = dict(call["arguments"])
        if call["name"] == "execute_smart_mutation":
            args["commit_group_id"] = group_id

        try:
            tool_result = await mcp_session.call_tool(call["name"], args)
            result_text = ""
            if hasattr(tool_result, "content"):
                for block in tool_result.content:
                    if block.type == "text":
                        result_text += block.text
                    else:
                        result_text += str(block)
            else:
                result_text = str(tool_result)
            if "error" in result_text.lower() and '"status": "success"' not in result_text.lower():
                error_context = f"Previous attempt failed: {result_text}"
                continue
            return {"status": "success", "result": result_text}
        except Exception as exc:
            error_context = f"Previous attempt failed with exception: {exc}"

    return {"status": "error", "result": f"Auto-healing failed after {max_retries} attempts. Last error: {error_context}"}
