#!/usr/bin/env python
"""Small, evidence-backed eval pass for the db-agent safety layer.

Runs the guardrail + gate classification against a curated set of SQL
statements an LLM worker would emit for common natural-language requests
(see ``eval/queries.json``). Each entry carries the NL intent (for the README
table) and the SQL the guardrails actually see.

This is deliberately NOT a full LLM harness: it validates, deterministically
and without network access, that the safety layer classifies each pattern the
way the README claims.

Usage:
    .venv/bin/python eval/run_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from db_agent.guardrails import classify_risk, classify_statement, is_safe_read_query, validate_where_condition

QUERIES_PATH = Path(__file__).parent / "queries.json"


def _decide(sql: str) -> tuple[str, str]:
    """Return (statement_kind, safety_outcome) for a SQL statement.

    Mirrors the CLI gate's logic: structural safety first (read queries must
    be single SELECTs, WHERE fragments must be clean), then risk
    classification. DDL/unsafe patterns are blocked outright; risky mutations
    must be gated.
    """
    operation = classify_statement(sql)
    if operation == "DDL":
        return "blocked", "block"

    if operation == "SELECT":
        ok, _ = is_safe_read_query(sql)
        return ("select", "allow") if ok else ("blocked", "block")

    if operation == "UNKNOWN":
        return "blocked", "block"

    # Extract a rough WHERE for risk classification (a real worker passes the
    # structured where_condition argument; here we approximate from the SQL).
    # Do NOT split on ";" — the where validator must see the full fragment so
    # statement terminators inside it are caught.
    where = ""
    if " WHERE " in sql.upper():
        where = sql.upper().split(" WHERE ", 1)[1].strip()
    where_ok, _ = validate_where_condition(where)
    if not where_ok:
        return "blocked", "block"

    risk = classify_risk(operation, where)
    if risk.risky:
        return operation.lower(), "gate"
    return operation.lower(), "allow"


def run_eval() -> list[dict]:
    entries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    results = []
    for entry in entries:
        kind, outcome = _decide(entry["sql"])
        passed = outcome == entry["safety"] and kind == entry["expected"]
        results.append(
            {
                "id": entry["id"],
                "query": entry["query"],
                "expected": entry["expected"],
                "expected_safety": entry["safety"],
                "classified_as": kind,
                "safety_outcome": outcome,
                "pass": passed,
            }
        )
    return results


def main() -> None:
    results = run_eval()
    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    print(f"{'ID':<20} {'expected':<10} {'safety':<8} {'classified':<12} result")
    print("-" * 70)
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(
            f"{r['id']:<20} {r['expected']:<10} {r['expected_safety']:<8} "
            f"{r['classified_as']:<12} {mark}"
        )
    print("-" * 70)
    print(f"Passed {passed}/{total}")

    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
