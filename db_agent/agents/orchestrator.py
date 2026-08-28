import json
from typing import Any

from db_agent.providers import ProviderAdapter
from db_agent.tracing import span


async def plan_dag(
    user_query: str,
    schema_text: str,
    adapter: ProviderAdapter,
    model: str,
) -> list[dict[str, Any]]:
    with span(
        "orchestrator.plan_dag",
        input={"query": user_query},
        metadata={"model": model},
    ) as obs:
        prompt = f"""You are a database query orchestrator. Your job is to break down complex natural language database requests into a JSON array of atomic operations.
Each operation must represent a single atomic intent (e.g., read data, insert one record, update one record, delete one record).

Current Database Schema:
{schema_text}

Rules:
1. Output ONLY a valid JSON array of objects. Do not include markdown formatting like ```json or conversational text.
2. Each object must have exactly these keys:
   - "id": integer (starting at 1)
   - "intent": string (A detailed natural language description of what to execute, including values to insert/update, or what to delete. For example, "Insert user John into users table with role Admin")
   - "depends_on": array of integers (IDs of steps that must finish before this one. Empty array if none.)
3. Operations that do not depend on each other should have empty "depends_on" arrays so they run in parallel.
4. If a step relies on data fetched from a previous step, that is a dependency. However, our system does not pass data between steps easily, so keep intents independent when possible. For independent mutations, use empty "depends_on".

User Request: {user_query}
"""
        try:
            result = await adapter.generate(
                prompt=prompt,
                model=model,
                temperature=0.1,
                response_format="json",
            )
            dag = json.loads(result.text)
            if not isinstance(dag, list):
                dag = [dag]
            obs.end(output={"task_count": len(dag), "tasks": dag})
            return dag
        except Exception:
            fallback = [{"id": 1, "intent": user_query, "depends_on": []}]
            obs.end(output={"task_count": 1, "tasks": fallback, "fallback": True})
            return fallback
