"""Lille SQLite-database med status for hvert emne og programmets egne udgifter."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,          -- fx "movie:12" eller "episode:345"
    media_type TEXT NOT NULL,
    media_id INTEGER NOT NULL,
    title TEXT,
    status TEXT NOT NULL,          -- i_gang, faerdig, forsoeg_igen, fejlet, sprunget_over
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    video_path TEXT,
    source TEXT,
    da_path TEXT,
    started_at TEXT,
    updated_at TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    flags_before INTEGER,
    flags_after INTEGER,
    fixes INTEGER
);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    month TEXT NOT NULL,
    item_key TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def month_key(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m")


class Database:
    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone()

    def upsert(self, key: str, **fields) -> None:
        fields["updated_at"] = iso()
        row = self.get(key)
        if row is None:
            cols = ["key", *fields]
            self.conn.execute(
                f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (key, *fields.values()),
            )
        else:
            sets = ", ".join(f"{c} = ?" for c in fields)
            self.conn.execute(f"UPDATE items SET {sets} WHERE key = ?", (*fields.values(), key))
        self.conn.commit()

    def with_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM items WHERE status = ?", (status,)))

    def add_spend(self, item_key: str | None, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.conn.execute(
            "INSERT INTO spend (ts, month, item_key, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (iso(), month_key(), item_key, input_tokens, output_tokens, cost),
        )
        self.conn.commit()

    def month_spend(self, month: str | None = None) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE month = ?", (month or month_key(),)
        ).fetchone()
        return float(row[0])
