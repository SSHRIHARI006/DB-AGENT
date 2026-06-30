# db-agent: Autonomous CLI Database Assistant

`db-agent` is a terminal-based autonomous database assistant. It allows you to query, mutate, inspect, and roll back SQL databases using natural language. The system operates fully locally, ensuring privacy and reliability by combining a local LLM with the Model Context Protocol (MCP).

---

## Key Features

- **Natural Language Interaction**: Query (`SELECT`) and mutate (`INSERT`, `UPDATE`, `DELETE`) your database using conversational English.
- **State Timeline & Time-Travel Rollbacks**:
  - `/log`: Displays a detailed audit timeline of database changes made in the session.
  - `/undo`: Safely rolls back the last mutation transaction.
  - `/revert <hash>`: Sequentially unwinds database records back to a specific commit checkpoint.
- **Interactive Workspace & Session Manager**:
  - Arrow-key navigation menu to browse and select existing sessions.
  - Clean isolated session contexts (`session_config.json`, history, and transaction logs).
  - Hotkey session deletion (`d`/`D` with confirmation).
- **Execution Safety & Guardrails**:
  - **DDL Blocking**: Structural schema modifications (`CREATE TABLE`, `DROP`, `ALTER`) are strictly blocked for security.
  - **Turn-Level Deduplication**: Intercepts repeating failed queries to prevent infinite agent tool loops.
- **Multi-Database Support**: Out-of-the-box support for SQLite, PostgreSQL, MySQL, and other SQLAlchemy-supported databases.

---

## Prerequisites

1. **Python**: `3.12` or higher.
2. **Ollama**: A local instance of Ollama running on your PC.
   - [Download Ollama](https://ollama.com/)
   - Pull the default local coding model:
     ```bash
     ollama pull qwen2.5-coder:1.5b
     ```
3. **Database Server**: A local or remote database (SQLite, PostgreSQL, MySQL) to connect to.

---

## Quick Start (Seamless Setup)

Simply clone the repository and run the launcher. The installer will automatically configure Ollama, start the service, pull the `qwen2.5-coder:1.5b` model, set up a virtual environment, install dependencies, and **globally link the command** to `~/.local/bin/db-agent`:

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
./db-agent
```

### Run from Anywhere
Once the setup is complete, you can type `db-agent` from any directory in your terminal to start the assistant:
```bash
db-agent
```
*(Ensure `~/.local/bin` is in your system's PATH variable).*

---

## Alternative Usage

### Option B: Connect Directly to a Database
```bash
db-agent postgresql://username:password@localhost/dbname --session my_pg_session
```

---

## Chat Loop Special Commands

Inside the natural language chat prompt, you can use these special commands to control your database state:

| Command | Description |
| :--- | :--- |
| `/log` | Renders a styled table detailing session transaction history & commit hashes. |
| `/undo` | Rolls back the single most recent database mutation. |
| `/revert <hash>` | Rolls back all mutations sequentially up to the specified commit hash. |
| `/exit` or `exit` | Safely disconnects the active database connection and returns to the main menu. |

---

## Database Driver Packages
The application installs the following drivers by default:
- **SQLite**: Built-in Python library.
- **PostgreSQL**: `psycopg2-binary`
- **MySQL / MariaDB**: `pymysql`
