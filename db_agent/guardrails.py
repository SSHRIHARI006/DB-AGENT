"""SQL guardrails: statement classification, validation, and risk scoring.

This module replaces the naive substring keyword checks in ``mcp_server.py``
with conservative checks that strip comments and string literals before
keyword matching. It is a **blacklist-based** control — see the README's
security notes for the documented residual risk. It is deliberately not a
full SQL parser; structural validation is future work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

StatementKind = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "DDL", "UNKNOWN"]

# DDL keywords that are always blocked, regardless of position.
_DDL_KEYWORDS = {
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "RENAME",
    "REPLACE",
    "VACUUM",
    "ATTACH",
    "DETACH",
}

# Forbidden substrings in a WHERE fragment. This is a blacklist: it blocks
# known-bad patterns, not unknown ones. Documented as a residual risk.
_WHERE_FORBIDDEN = (
    ";",
    "--",
    "/*",
    "*/",
    "UNION",
    "EXEC",
    "PRAGMA",
    "ATTACH",
    "DETACH",
)

_MAX_WHERE_LENGTH = 500

# Comment/string-literal stripping: remove '...', "...", `...`, -- ... and
# /* ... */ before keyword matching, so "DROP" inside a string literal does
# not trigger a false positive.
_STRIP_PATTERN = re.compile(
    r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`|--[^\n]*|/\*.*?\*/",
    re.DOTALL,
)

_SELECT_PREFIX = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)


def strip_sql_comments_and_literals(sql: str) -> str:
    """Remove string literals and comments so keyword checks see real tokens."""
    return _STRIP_PATTERN.sub(" ", sql or "")


def classify_statement(sql: str) -> StatementKind:
    """Classify a single SQL statement by its leading keyword.

    ``INSERT INTO``, ``UPDATE``, ``DELETE FROM`` and ``SELECT`` map to their
    statement kinds; anything else (or a statement that parses to nothing) is
    ``DDL`` if it contains a DDL keyword, otherwise ``UNKNOWN``.
    """
    cleaned = strip_sql_comments_and_literals(sql or "")
    upper = cleaned.upper()
    first_word_match = re.match(r"^\s*([A-Z]+)", upper)
    first_word = first_word_match.group(1) if first_word_match else ""

    if first_word in {"SELECT", "WITH"}:
        return "SELECT"
    if first_word == "INSERT":
        return "INSERT"
    if first_word == "UPDATE":
        return "UPDATE"
    if first_word == "DELETE":
        return "DELETE"

    tokens = set(re.findall(r"\b[A-Z]+\b", upper))
    if tokens & _DDL_KEYWORDS:
        return "DDL"
    if first_word:
        return "UNKNOWN"
    return "UNKNOWN"


def validate_table_identifier(name: str, inspector) -> tuple[bool, str]:
    """Return ``(ok, reason)``. The table must exist in the reflected schema."""
    if not name or not isinstance(name, str):
        return False, "Table name must be a non-empty string."
    if len(name) > 128:
        return False, "Table name is unreasonably long."
    tables = set(inspector.get_table_names())
    if name not in tables:
        return False, f"Table '{name}' does not exist in the reflected schema."
    return True, ""


def validate_where_condition(where: str) -> tuple[bool, str]:
    """Blacklist check for a WHERE fragment. Returns ``(ok, reason)``.

    Blocks statement terminators, comment markers, and verbs that indicate
    sub-queries or engine control. Does **not** fully parse the fragment —
    that is a documented residual risk.
    """
    if where is None:
        return True, ""
    where = where.strip()
    if not where:
        return True, ""
    if len(where) > _MAX_WHERE_LENGTH:
        return False, f"WHERE condition exceeds {_MAX_WHERE_LENGTH} characters."
    upper = where.upper()
    for token in _WHERE_FORBIDDEN:
        if token in upper:
            return False, f"WHERE condition contains forbidden token '{token}'."
    return True, ""


@dataclass(frozen=True)
class Risk:
    risky: bool
    reason: str = ""


def classify_risk(
    operation: str,
    where_condition: str,
    *,
    table: str = "",
    allowed_tables: list[str] | None = None,
) -> Risk:
    """Classify the risk of a mutation.

    Risky if:
    - the operation is DDL (any structural change)
    - the operation is DELETE
    - the operation is UPDATE with a blank WHERE (would hit every row)
    - the table is not in ``allowed_tables`` (when an allowlist is configured)
    """
    op = (operation or "").upper()
    if op == "DDL":
        return Risk(True, "DDL (structural database modification) is always risky.")
    if op == "DELETE":
        return Risk(True, "DELETE is a destructive operation.")
    if op == "UPDATE" and not (where_condition or "").strip():
        return Risk(True, "UPDATE without a WHERE condition affects every row.")
    if allowed_tables and table and table not in allowed_tables:
        return Risk(True, f"Table '{table}' is not in the allowed table list.")
    return Risk(False, "")


def is_safe_read_query(sql: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``. Must be a single SELECT/WITH statement with no
    mutating or structural keywords anywhere in it."""
    cleaned = strip_sql_comments_and_literals(sql or "")
    if not cleaned.strip():
        return False, "Query is empty."
    if not _SELECT_PREFIX.match(cleaned):
        return False, "Only SELECT statements are allowed for read queries."
    upper = cleaned.upper()
    if ";" in upper.strip().rstrip(";"):
        inner = upper.strip().rstrip(";")
        if ";" in inner:
            return False, "Multiple statements are not allowed."
    tokens = set(re.findall(r"\b[A-Z]+\b", upper))
    forbidden = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "RENAME", "TRUNCATE", "REPLACE"}
    found = tokens & forbidden
    if found:
        return False, f"Read query contains a forbidden keyword: {', '.join(sorted(found))}."
    return True, ""
