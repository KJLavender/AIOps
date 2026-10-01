"""SQLite: daily digests, repos already recommended, research topics."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

from .github import Repo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
    day TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    items TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    full_name TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    # --- digests -----------------------------------------------------------
    def save_digest(self, day: str, repos: list[Repo]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO digests (day, created_at, items) VALUES (?, ?, ?)",
                (day, time.time(), json.dumps([r.to_dict() for r in repos], ensure_ascii=False)),
            )
            self._db.commit()

    def digest(self, day: str) -> Optional[list[dict]]:
        with self._lock:
            row = self._db.execute("SELECT items FROM digests WHERE day = ?", (day,)).fetchone()
        return json.loads(row["items"]) if row else None

    def recent_digests(self, limit: int = 14) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT day, created_at, items FROM digests ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"day": r["day"], "created_at": r["created_at"], "items": json.loads(r["items"])}
                for r in rows]

    # --- recommendations already made --------------------------------------
    def seen(self) -> set[str]:
        with self._lock:
            return {r["full_name"] for r in self._db.execute("SELECT full_name FROM seen")}

    def mark_seen(self, names: list[str]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO seen (full_name, first_seen) VALUES (?, ?)",
                [(n, time.time()) for n in names],
            )
            self._db.commit()

    # --- topics --------------------------------------------------------------
    def topics(self) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._db.execute("SELECT id, query FROM topics ORDER BY id").fetchall()
        return [(r["id"], r["query"]) for r in rows]

    def add_topic(self, query: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO topics (query, created_at) VALUES (?, ?)", (query, time.time()))
            self._db.commit()
            return cur.rowcount > 0

    def remove_topic(self, topic_id: int) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT query FROM topics WHERE id = ?", (topic_id,)).fetchone()
            if row:
                self._db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
                self._db.commit()
        return row["query"] if row else None
