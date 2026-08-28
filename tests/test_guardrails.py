import pytest

from db_agent.guardrails import (
    classify_risk,
    classify_statement,
    is_safe_read_query,
    strip_sql_comments_and_literals,
    validate_table_identifier,
    validate_where_condition,
)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM users", "SELECT"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT"),
        ("INSERT INTO users VALUES (1)", "INSERT"),
        ("UPDATE users SET name = 'x'", "UPDATE"),
        ("DELETE FROM users", "DELETE"),
        ("DROP TABLE users", "DDL"),
        ("ALTER TABLE users ADD COLUMN x", "DDL"),
        ("CREATE TABLE x (id INT)", "DDL"),
        ("TRUNCATE TABLE users", "DDL"),
        ("   SELECT id FROM users  ", "SELECT"),
        ("", "UNKNOWN"),
        ("SELECT 'DROP TABLE users'", "SELECT"),
        ("-- DROP TABLE users\nSELECT 1", "SELECT"),
    ],
)
def test_classify_statement(sql, expected):
    assert classify_statement(sql) == expected


def test_classify_statement_ignores_keywords_in_strings_and_comments():
    # "DROP" inside a string literal must not classify as DDL
    assert classify_statement("INSERT INTO logs (msg) VALUES ('DROP TABLE users')") == "INSERT"
    # "UPDATE" inside a comment must not classify as UPDATE
    assert classify_statement("SELECT 1 -- UPDATE users SET x=1") == "SELECT"


def test_strip_sql_comments_and_literals():
    stripped = strip_sql_comments_and_literals("SELECT 'DROP' -- comment\nFROM t")
    assert "DROP" not in stripped.upper()
    assert "--" not in stripped


@pytest.mark.parametrize(
    "where,ok",
    [
        ("id = 1", True),
        ("name = 'Bob' AND age > 30", True),
        ("", True),
        (None, True),
        ("id = 1; DROP TABLE users", False),
        ("id = 1 -- comment", False),
        ("id = 1 /* block */", False),
        ("1=1 UNION SELECT 1", False),
        ("PRAGMA table_info(users)", False),
        ("ATTACH 'x' AS y", False),
        ("DETACH y", False),
        ("EXEC sp_help", False),
        ("x" * 600, False),
    ],
)
def test_validate_where_condition(where, ok):
    valid, _ = validate_where_condition(where)
    assert valid is ok


def test_validate_table_identifier(monkeypatch, tmp_path):
    import sqlite3

    database = tmp_path / "t.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)

    ok, _ = validate_table_identifier("users", inspector)
    assert ok is True
    ok, reason = validate_table_identifier("missing_table", inspector)
    assert ok is False
    assert "does not exist" in reason
    ok, _ = validate_table_identifier("", inspector)
    assert ok is False


@pytest.mark.parametrize(
    "operation,where,table,allowed,expected_risky",
    [
        ("DELETE", "id = 1", "users", None, True),
        ("UPDATE", "", "users", None, True),
        ("UPDATE", "id = 1", "users", None, False),
        ("INSERT", "id = 1", "users", None, False),
        ("DDL", "", "users", None, True),
        ("SELECT", "", "users", None, False),
        ("UPDATE", "id = 1", "secret_table", ["users", "products"], True),
        ("UPDATE", "id = 1", "users", ["users", "products"], False),
    ],
)
def test_classify_risk(operation, where, table, allowed, expected_risky):
    risk = classify_risk(operation, where, table=table, allowed_tables=allowed)
    assert risk.risky is expected_risky


@pytest.mark.parametrize(
    "sql,ok",
    [
        ("SELECT * FROM users", True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", True),
        ("SELECT 'DELETE' FROM users", True),
        ("INSERT INTO users VALUES (1)", False),
        ("UPDATE users SET x = 1", False),
        ("DELETE FROM users", False),
        ("DROP TABLE users", False),
        ("SELECT 1; SELECT 2", False),
        ("", False),
    ],
)
def test_is_safe_read_query(sql, ok):
    valid, _ = is_safe_read_query(sql)
    assert valid is ok
