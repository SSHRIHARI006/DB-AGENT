import os
import json
import re
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, text, inspect
from db_agent.tracker import add_query_history_entry, add_rollback_entry

mcp = FastMCP("DB_Core")

def get_engine():
    db_uri = os.environ.get("DYNAMIC_DB_URI")
    if not db_uri:
        raise ValueError("DYNAMIC_DB_URI environment variable is not set.")
    return create_engine(db_uri)

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
    upper_query = sql_query.strip().upper()
    
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "RENAME", "TRUNCATE", "REPLACE"]
    for word in forbidden:
        if re.search(rf"\b{word}\b", upper_query):
            return f"Error: The query contains a mutating keyword '{word}'. Only read-only queries are allowed."
            
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
def execute_smart_mutation(table_name: str, sql_query: str, where_condition: str, commit_group_id: str = None) -> str:
    upper_query = sql_query.strip().upper()
    
    if "DROP" in upper_query or "ALTER" in upper_query:
        return "Error: Structural database modifications (DDL) are blocked by this agent."
        
    operation = None
    if "INSERT" in upper_query:
        operation = "INSERT"
    elif "UPDATE" in upper_query:
        operation = "UPDATE"
    elif "DELETE" in upper_query:
        operation = "DELETE"
        
    if not operation:
        return "Error: Could not identify operation type (INSERT, UPDATE, DELETE) in the query."
        
    session_name = os.environ.get("SESSION_NAME", "default")
    
    try:
        engine = get_engine()
        is_sqlite = engine.dialect.name == "sqlite"
        
        inspector = inspect(engine)
        pk_constraint = inspector.get_pk_constraint(table_name)
        primary_keys = pk_constraint.get("constrained_columns", []) if pk_constraint else []
        
        before_rows = []
        
        with engine.begin() as conn:
            if operation in ("UPDATE", "DELETE"):
                where_clause = f" WHERE {where_condition}" if where_condition.strip() else ""
                lock_clause = "" if is_sqlite else " FOR UPDATE"
                snapshot_sql = f"SELECT * FROM {table_name}{where_clause}{lock_clause}"
                
                snapshot_result = conn.execute(text(snapshot_sql))
                before_rows = [dict(row._mapping) for row in snapshot_result.all()]
                
            result = conn.execute(text(sql_query))
            
            if operation == "INSERT":
                where_clause = f" WHERE {where_condition}" if where_condition.strip() else ""
                snapshot_sql = f"SELECT * FROM {table_name}{where_clause}"
                
                snapshot_result = conn.execute(text(snapshot_sql))
                before_rows = [dict(row._mapping) for row in snapshot_result.all()]
                
        commit_hash = add_rollback_entry(
            session_name=session_name,
            operation=operation,
            table=table_name,
            query=sql_query,
            before=before_rows,
            primary_keys=primary_keys,
            commit_group_id=commit_group_id
        )
        
        return json.dumps({
            "status": "success",
            "commit_hash": commit_hash,
            "operation": operation,
            "table": table_name,
            "rows_affected": len(before_rows),
            "message": f"Mutation completed successfully with commit hash {commit_hash}."
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Transaction failed and was rolled back: {str(e)}"
        }, indent=2)

if __name__ == "__main__":
    mcp.run()
