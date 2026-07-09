import json
import ollama
import re
from typing import Dict, Any

def parse_fallback_tool_calls(content: str) -> list[dict]:
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

async def execute_task(task: dict, schema_text: str, group_id: str, mcp_session, max_retries=3) -> dict:
    intent = task.get("intent", "")
    
    mcp_tools = await mcp_session.list_tools()
    ollama_tools = []
    valid_tool_names = []
    for t in mcp_tools.tools:
        valid_tool_names.append(t.name)
        tool_schema = {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.inputSchema
            }
        }
        ollama_tools.append(tool_schema)
        
    client = ollama.AsyncClient(host="http://localhost:11434")
    model_name = "qwen2.5-coder:0.5b"
    
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
            )
        }
    ]
    
    for attempt in range(max_retries):
        current_msg = f"Intent: {intent}\n"
        if error_context:
            current_msg += f"{error_context}\nFix the SQL and try again."
            
        temp_messages = messages + [{"role": "user", "content": current_msg}]
        
        try:
            response = await client.chat(
                model=model_name,
                messages=temp_messages,
                tools=ollama_tools,
                options={"temperature": 0.1}
            )
        except Exception as e:
            return {"status": "error", "result": f"LLM Connection Error: {str(e)}"}
            
        msg = response.message
        calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.function.name in valid_tool_names:
                    calls.append({
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    })
        else:
            calls = parse_fallback_tool_calls(msg.content)
            
        if not calls:
            error_context = "Error: You did not output a valid tool call. You MUST call a tool like execute_smart_mutation or read_query."
            continue
            
        # Execute the first valid tool call found
        call = calls[0]
        args = call["arguments"]
        
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
                
            if "error" in result_text.lower() and "status" not in result_text.lower():
                error_context = f"Previous attempt failed: {result_text}"
                continue
                
            return {"status": "success", "result": result_text}
            
        except Exception as e:
            error_context = f"Previous attempt failed with exception: {str(e)}"
            continue
            
    return {"status": "error", "result": f"Auto-healing failed after {max_retries} attempts. Last error: {error_context}"}
