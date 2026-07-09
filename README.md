# DB-Agent v2: Autonomous Hierarchical Database Assistant

`db-agent` is a terminal-based autonomous database assistant. It allows you to query, mutate, inspect, and roll back SQL databases using natural language. The v2 architecture introduces a **Hierarchical Multi-Agent System** that breaks down complex requests into actionable DAGs (Directed Acyclic Graphs), operating entirely locally for maximum privacy.

---

## Key Features

- **Multi-Agent Architecture**: 
  - **Orchestrator Agent**: A `7B` model parses complex, multi-step queries into a JSON DAG of atomic tasks.
  - **Worker Agent**: A fast `0.5B` model executes each task in the DAG asynchronously, complete with an auto-healing feedback loop for syntax errors.
- **Natural Language Interaction**: Process complex queries containing multiple reads and mutations (`INSERT`, `UPDATE`, `DELETE`) in a single prompt.
- **State Timeline & Group Rollbacks**:
  - `/log`: Displays a detailed audit timeline of database changes.
  - `/undo`: Safely rolls back the *entire group* of mutations from your last complex query simultaneously.
  - `/revert <hash>`: Sequentially unwinds database records back to a specific commit checkpoint.
- **Interactive Workspace & Session Manager**:
  - Arrow-key navigation menu to browse and select existing sessions.
- **Cross-Platform Compatibility**: A unified Python installation wizard supporting Windows, macOS, and Linux.

---

## Prerequisites

1. **Python**: `3.12` or higher.
2. **Ollama**: A local instance of Ollama running on your machine.
   - [Download Ollama](https://ollama.com/)

---

## Quick Start (Seamless Setup)

Simply clone the repository and run the cross-platform setup wizard. It will automatically verify Python, download Ollama (if missing on Linux), pull the required models (`qwen2.5-coder:7b` & `0.5b`), and link the `db-agent` command globally:

```bash
git clone https://github.com/SSHRIHARI006/DB-AGENT.git db-agent
cd db-agent
python install.py
```

### Run from Anywhere
Once the setup is complete, you can type `db-agent` from any directory in your terminal to start the assistant:
```bash
db-agent
```
*(Ensure `~/.local/bin` (Linux/Mac) or your Python Scripts folder (Windows) is in your system's PATH variable).*

---

## Alternative Usage

### Connect Directly to a Database
```bash
db-agent sqlite:///test.db --session my_project
```

---

## Chat Loop Special Commands

Inside the natural language chat prompt, you can use these special commands to control your database state:

| Command | Description |
| :--- | :--- |
| `/log` | Renders a styled table detailing session transaction history & group commit hashes. |
| `/undo` | Rolls back all mutations originating from the single most recent query. |
| `/revert <hash>` | Rolls back all mutations sequentially up to the specified commit hash. |
| `/exit` or `exit` | Safely disconnects the active database connection and returns to the main menu. |

---

## Database Driver Packages
The application installs the following drivers by default:
- **SQLite**: Built-in Python library.
- **PostgreSQL**: `psycopg2-binary`
- **MySQL / MariaDB**: `pymysql`
