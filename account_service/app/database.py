import os
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "account.db"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.getenv("ACCOUNT_DB_PATH") or str(DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            event_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('CREDIT', 'DEBIT')),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            currency TEXT NOT NULL,
            event_timestamp TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_account_time "
        "ON transactions(account_id, event_timestamp)"
    )
    conn.commit()
