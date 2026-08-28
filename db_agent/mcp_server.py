import os
import json
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, text, inspect
from db_agent.guardrails import (
    classify_statement,
    is_safe_read_query,
    validate_table_identifier,
    validate_where_condition,
)
from db_agent.tracker import (
    add_query_history_entry,
    build_rollback_entry,
    persist_rollback_entry,
)

mcp = FastMCP("DB_Core")

def get_engine(db_uri: str | None = None):
    db_uri = db_uri or os.environ.get("DYNAMIC_DB_URI")
    if not db_uri:
        raise ValueError("No database URI provided and DYNAMIC_DB_URI environment variable is not set.")
    return create_engine(db_uri)


def conn_quoted_table(engine, table_name: str) -> str:
    """Quote an identifier with the dialect's preparer instead of splicing it raw."""
    return engine.dialect.identifier_preparer.quote(table_name)

@mcp.resource("schema://current")
def get_schema() -> str:
    try:
        engine = get_engine()
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        
        if not tables:
            return "Database is empty (no tables found)."
            
        schema_desc = []
        for table in tables:
            columns = inspector.get_columns(table)
            pk_constraint = inspector.get_pk_constraint(table)
            pk_cols = pk_constraint.get("constrained_columns", []) if pk_constraint else []
            
            cols_desc = []
            for col in columns:
                col_name = col["name"]
                col_type = str(col["type"])
                is_nullable = col.get("nullable", True)
                pk_marker = " (Primary Key)" if col_name in pk_cols else ""
                null_marker = " NULL" if is_nullable else " NOT NULL"
                cols_desc.append(f"  - {col_name}: {col_type}{pk_marker}{null_marker}")
                
            schema_desc.append(f"Table: {table}\n" + "\n".join(cols_desc))
            
        return "\n\n".join(schema_desc)
    except Exception as e:
        return f"Error reflecting database schema: {str(e)}"

@mcp.tool()
def read_query(sql_query: str) -> str:
    ok, reason = is_safe_read_query(sql_query)
    if not ok:
        return f"Error: {reason}"

    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = [dict(row._mapping) for row in result.all()] if result.returns_rows else []
            add_query_history_entry(
                os.environ.get("SESSION_NAME", "default"),
                "SELECT",
                "query",
                sql_query,
                len(rows),
                rollbackable=False,
            )
            if result.returns_rows:
                return json.dumps(rows, default=str, indent=2)
            return "Query executed successfully, but returned no rows."
    except Exception as e:
        return f"Error executing read query: {str(e)}"

@mcp.tool()
def execute_smart_mutation(
    table_name: str,
    sql_query: str,
    where_condition: str,
    commit_group_id: str = None,
    approved_via: str = "manual",
) -> str:
    operation = classify_statement(sql_query)

    if operation == "DDL":
        return "Error: Structural database modifications (DDL) are blocked by this agent."

    if operation not in ("INSERT", "UPDATE", "DELETE"):
        return "Error: Could not identify operation type (INSERT, UPDATE, DELETE) in the query."

    session_name = os.environ.get("SESSION_NAME", "default")

    try:
        engine = get_engine()
        is_sqlite = engine.dialect.name == "sqlite"
        inspector = inspect(engine)

        table_ok, table_reason = validate_table_identifier(table_name, inspector)
        if not table_ok:
            return f"Error: {table_reason}"

        where_ok, where_reason = validate_where_condition(where_condition or "")
        if not where_ok:
            return f"Error: {where_reason}"

        if operation == "INSERT" and not (where_condition or "").strip():
            return (
                "Error: INSERT rollback capture requires a non-empty where_condition so /undo "
                "can target exactly the inserted row. Refusing to snapshot the whole table."
            )

        pk_constraint = inspector.get_pk_constraint(table_name)
        primary_keys = pk_constraint.get("constrained_columns", []) if pk_constraint else []

        before_rows = []
        quoted_table = conn_quoted_table(engine, table_name)

        with engine.begin() as conn:
            if operation in ("UPDATE", "DELETE"):
                where_clause = f" WHERE {where_condition}" if where_condition.strip() else ""
                lock_clause = "" if is_sqlite else " FOR UPDATE"
                snapshot_sql = f"SELECT * FROM {quoted_table}{where_clause}{lock_clause}"

                snapshot_result = conn.execute(text(snapshot_sql))
                before_rows = [dict(row._mapping) for row in snapshot_result.all()]

            result = conn.execute(text(sql_query))

            if operation == "INSERT":
                where_clause = f" WHERE {where_condition}" if where_condition.strip() else ""
                snapshot_sql = f"SELECT * FROM {quoted_table}{where_clause}"

                snapshot_result = conn.execute(text(snapshot_sql))
                before_rows = [dict(row._mapping) for row in snapshot_result.all()]

            # Write the rollback entry inside the transaction: a JSON write
            # failure here raises and rolls back the DB commit, so we never
            # leave a committed mutation with no rollback record.
            entry = build_rollback_entry(
                operation=operation,
                table=table_name,
                query=sql_query,
                before=before_rows,
                primary_keys=primary_keys,
                commit_group_id=commit_group_id,
                approved_via=approved_via,
            )
            commit_hash = persist_rollback_entry(session_name, entry)

        return json.dumps({
            "status": "success",
            "commit_hash": commit_hash,
            "operation": operation,
            "table": table_name,
            "rows_affected": len(before_rows),
            "message": f"Mutation completed successfully with commit hash {commit_hash}.",
        }, indent=2)

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Transaction failed and was rolled back: {str(e)}"
        }, indent=2)

if __name__ == "__main__":
    mcp.run()
