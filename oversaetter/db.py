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
    status TEXT NOT NULL,          -- soeger, soegt, mangler_engelsk, i_gang, faerdig, forsoeg_igen, fejlet,
                                   -- sprunget_over, ikke_muligt
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
    fixes INTEGER,
    search_started_at TEXT,        -- Bazarr er bedt om at søge efter dansk (søgningen er ikke vurderet endnu)
    search_jobs TEXT,              -- id'er på Bazarrs søgejob, kommasepareret
    search_at TEXT,                -- tidspunkt for en gyldig søgning (ingen tjeneste på pause før og efter)
    en_search_at TEXT,             -- tidspunkt for seneste gyldige søgning efter engelsk (ingen dansk, ingen kilde)
    series_id INTEGER,             -- sonarrSeriesId (kun afsnit), så ordlisten kan genbruges i serien
    glossary TEXT,                 -- ordlisten (JSON) fra GLOSSARY=on
    en_hi INTEGER                  -- 1 = oversat fra en engelsk HI/SDH-kilde, 0 = almindelig (NULL = ikke vurderet)
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
CREATE TABLE IF NOT EXISTS providers (
    name TEXT PRIMARY KEY,         -- Bazarrs navn på tjenesten, fx "opensubtitlescom"
    paused_since TEXT NOT NULL     -- første gang vi så den sat på pause (i træk)
);
"""

# Kolonner tilføjet efter første version. Ældre databaser får dem automatisk, uden at miste rækker.
NEW_COLUMNS = {
    "items": {
        "search_started_at": "TEXT",
        "search_jobs": "TEXT",
        "search_at": "TEXT",
        "en_search_at": "TEXT",
        "series_id": "INTEGER",
        "glossary": "TEXT",
        "en_hi": "INTEGER",
    }
}


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
        # Parallelle bidder registrerer udgifter fra flere tråde; kaldene serialiseres af oversættelsens lås.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._upgrade()
        self.conn.commit()

    def _upgrade(self) -> None:
        for table, columns in NEW_COLUMNS.items():
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

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

    def series_glossaries(self, series_id: int, exclude_key: str, limit: int = 5) -> list[str]:
        """Ordlister (JSON) fra tidligere afsnit af samme serie, nyeste først."""
        rows = self.conn.execute(
            "SELECT glossary FROM items WHERE series_id = ? AND key != ? AND glossary IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (series_id, exclude_key, limit),
        )
        return [r["glossary"] for r in rows]

    def track_paused(self, paused: list[str], when: datetime | None = None) -> dict[str, datetime]:
        """Husk hvornår hver tjeneste blev sat på pause. Returnerer navn -> siden hvornår (kun dem på pause nu)."""
        when = when or now()
        self.conn.execute(f"DELETE FROM providers WHERE name NOT IN ({', '.join('?' for _ in paused)})", tuple(paused))
        for name in paused:
            self.conn.execute("INSERT OR IGNORE INTO providers (name, paused_since) VALUES (?, ?)", (name, iso(when)))
        self.conn.commit()
        rows = self.conn.execute("SELECT name, paused_since FROM providers").fetchall()
        return {r["name"]: datetime.fromisoformat(r["paused_since"]) for r in rows}

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
