"""SQLite persistence: watched routes, price history, chat log."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_name TEXT NOT NULL,
    origin_codes TEXT NOT NULL,
    dest_name TEXT NOT NULL,
    dest_codes TEXT NOT NULL,
    date_from TEXT,
    date_to TEXT,
    stay_days INTEGER,
    max_price REAL,
    nonstop INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_checked_at REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES watches(id),
    checked_at REAL NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    return_date TEXT,
    airline TEXT,
    flight_no TEXT,
    depart_time TEXT,
    stops INTEGER
);
CREATE INDEX IF NOT EXISTS prices_watch ON prices(watch_id, checked_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


@dataclass
class Watch:
    id: int
    origin_name: str
    origin_codes: list[str]
    dest_name: str
    dest_codes: list[str]
    date_from: Optional[str] = None  # None = rolling window
    date_to: Optional[str] = None
    stay_days: Optional[int] = None  # set = round trip
    max_price: Optional[float] = None
    nonstop: bool = False
    active: bool = True
    note: str = ""
    created_at: float = 0.0
    last_checked_at: Optional[float] = None
    last_error: Optional[str] = None

    @property
    def route(self) -> str:
        return f"{self.origin_name}→{self.dest_name}"

    @property
    def codes_label(self) -> str:
        return f"{'/'.join(self.origin_codes)}→{'/'.join(self.dest_codes)}"

    def window(self, today: date, start_days: int, end_days: int) -> tuple[date, date]:
        """Dates to search. Fixed windows are clipped so they never start in the past."""
        if self.date_from and self.date_to:
            start = max(date.fromisoformat(self.date_from), today + timedelta(days=1))
            return start, date.fromisoformat(self.date_to)
        return today + timedelta(days=start_days), today + timedelta(days=end_days)

    def describe_window(self) -> str:
        if self.date_from and self.date_to:
            text = f"{self.date_from} ~ {self.date_to}"
        else:
            text = "未來日期（滾動）"
        if self.stay_days:
            text += f"，來回 {self.stay_days} 天"
        return text


@dataclass
class PricePoint:
    watch_id: int
    checked_at: float
    price: float
    currency: str
    travel_date: str
    return_date: Optional[str] = None
    airline: Optional[str] = None
    flight_no: Optional[str] = None
    depart_time: Optional[str] = None
    stops: Optional[int] = None


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    # --- watches -----------------------------------------------------------
    def add_watch(
        self,
        origin_name: str,
        origin_codes: list[str],
        dest_name: str,
        dest_codes: list[str],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        stay_days: Optional[int] = None,
        max_price: Optional[float] = None,
        nonstop: bool = False,
        note: str = "",
    ) -> Watch:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO watches (origin_name, origin_codes, dest_name, dest_codes, date_from,"
                " date_to, stay_days, max_price, nonstop, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (origin_name, ",".join(origin_codes), dest_name, ",".join(dest_codes), date_from,
                 date_to, stay_days, max_price, int(nonstop), note, time.time()),
            )
            self._db.commit()
            watch_id = cur.lastrowid
        return self.get_watch(watch_id)

    def get_watch(self, watch_id: int) -> Optional[Watch]:
        with self._lock:
            row = self._db.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        return _watch(row) if row else None

    def list_watches(self, active_only: bool = True) -> list[Watch]:
        query = "SELECT * FROM watches"
        if active_only:
            query += " WHERE active = 1"
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id").fetchall()
        return [_watch(r) for r in rows]

    def find_active(self, origin_codes: list[str], dest_codes: list[str]) -> list[Watch]:
        o, d = set(origin_codes), set(dest_codes)
        return [
            w for w in self.list_watches()
            if set(w.origin_codes) == o and set(w.dest_codes) == d
        ]

    def update_watch(self, watch_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self._lock:
            self._db.execute(f"UPDATE watches SET {cols} WHERE id = ?", (*values, watch_id))
            self._db.commit()

    # --- prices ------------------------------------------------------------
    def add_price(self, p: PricePoint) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO prices (watch_id, checked_at, price, currency, travel_date, return_date,"
                " airline, flight_no, depart_time, stops) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p.watch_id, p.checked_at, p.price, p.currency, p.travel_date, p.return_date,
                 p.airline, p.flight_no, p.depart_time, p.stops),
            )
            self._db.commit()

    def history(self, watch_id: int, limit: int = 200) -> list[PricePoint]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM prices WHERE watch_id = ? ORDER BY checked_at DESC LIMIT ?",
                (watch_id, limit),
            ).fetchall()
        return [_price(r) for r in reversed(rows)]

    def latest_price(self, watch_id: int) -> Optional[PricePoint]:
        points = self.history(watch_id, limit=1)
        return points[-1] if points else None

    def lowest_price(self, watch_id: int) -> Optional[float]:
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(price) AS low FROM prices WHERE watch_id = ?", (watch_id,)
            ).fetchone()
        return row["low"] if row and row["low"] is not None else None

    # --- chat log ----------------------------------------------------------
    def log_message(self, direction: str, source: str, text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (ts, direction, source, text) VALUES (?, ?, ?, ?)",
                (time.time(), direction, source, text),
            )
            self._db.commit()

    def recent_messages(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        origin_name=row["origin_name"],
        origin_codes=row["origin_codes"].split(","),
        dest_name=row["dest_name"],
        dest_codes=row["dest_codes"].split(","),
        date_from=row["date_from"],
        date_to=row["date_to"],
        stay_days=row["stay_days"],
        max_price=row["max_price"],
        nonstop=bool(row["nonstop"]),
        active=bool(row["active"]),
        note=row["note"],
        created_at=row["created_at"],
        last_checked_at=row["last_checked_at"],
        last_error=row["last_error"],
    )


def _price(row: sqlite3.Row) -> PricePoint:
    return PricePoint(
        watch_id=row["watch_id"],
        checked_at=row["checked_at"],
        price=row["price"],
        currency=row["currency"],
        travel_date=row["travel_date"],
        return_date=row["return_date"],
        airline=row["airline"],
        flight_no=row["flight_no"],
        depart_time=row["depart_time"],
        stops=row["stops"],
    )
