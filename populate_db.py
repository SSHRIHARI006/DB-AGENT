import argparse
import sqlite3
from pathlib import Path


def populate_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                stock INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            INSERT OR IGNORE INTO users VALUES
                (1, 'Alice', 'alice@example.com', 'admin'),
                (2, 'Bob', 'bob@example.com', 'customer'),
                (3, 'Carol', 'carol@example.com', 'customer');

            INSERT OR IGNORE INTO products VALUES
                (1, 'Keyboard', 79.99, 25),
                (2, 'Mouse', 29.50, 50),
                (3, 'Monitor', 249.00, 10);

            INSERT OR IGNORE INTO orders VALUES
                (1, 2, 'pending', '2026-08-08'),
                (2, 3, 'shipped', '2026-08-07');

            INSERT OR IGNORE INTO order_items VALUES
                (1, 1, 1, 1),
                (2, 1, 2, 2),
                (3, 2, 3, 1);
            """
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the db-agent SQLite test fixture.")
    parser.add_argument(
        "database",
        nargs="?",
        default="test.db",
        help="SQLite database path (default: test.db)",
    )
    args = parser.parse_args()
    database_path = Path(args.database).expanduser()
    populate_database(database_path)
    print(f"Test database ready: {database_path}")


if __name__ == "__main__":
    main()
