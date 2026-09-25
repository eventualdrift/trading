"""SQLite persistence for signals, positions, equity and bot state."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import fields
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pandas as pd

from .models import Position, Signal

_SQL_TYPES = {float: "REAL", int: "INTEGER", bool: "INTEGER", str: "TEXT", dict: "TEXT"}


def _base_type(hint) -> type:
    for t in (hint, *get_args(hint)):
        if t in _SQL_TYPES:
            return t
    return str


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._types = {cls: {k: _base_type(v) for k, v in get_type_hints(cls).items()} for cls in (Signal, Position)}
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_table("signals", Signal)
            self._ensure_table("positions", Position)
            self._conn.execute("CREATE TABLE IF NOT EXISTS equity (ts INTEGER, mode TEXT, equity REAL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    # ------------------------------------------------------------- plumbing
    def _ensure_table(self, table: str, cls) -> None:
        cols = [f.name for f in fields(cls) if f.name != "id"]
        types = self._types[cls]
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            + ", ".join(f"{c} {_SQL_TYPES[types[c]]}" for c in cols) + ")"
        )
        existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:  # simple forward migration
            if c not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} {_SQL_TYPES[types[c]]}")

    def _to_row(self, obj) -> dict[str, Any]:
        out = {}
        for f in fields(obj):
            if f.name == "id":
                continue
            v = getattr(obj, f.name)
            if isinstance(v, dict):
                v = json.dumps(v, default=float)
            elif isinstance(v, bool):
                v = int(v)
            out[f.name] = v
        return out

    def _from_row(self, cls, row: sqlite3.Row):
        types = self._types[cls]
        kwargs = {}
        for k in row.keys():
            v = row[k]
            t = types.get(k)
            if t is dict:
                v = json.loads(v) if v else {}
            elif t is bool and v is not None:
                v = bool(v)
            kwargs[k] = v
        return cls(**kwargs)

    def _insert(self, table: str, obj) -> int:
        row = self._to_row(obj)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
            obj.id = cur.lastrowid
            return obj.id

    def _update(self, table: str, obj) -> None:
        row = self._to_row(obj)
        with self._lock:
            self._conn.execute(
                f"UPDATE {table} SET {', '.join(f'{k}=?' for k in row)} WHERE id=?",
                [*row.values(), obj.id],
            )

    def _select(self, cls, sql: str, params=()) -> list:
        with self._lock:
            return [self._from_row(cls, r) for r in self._conn.execute(sql, params).fetchall()]

    # -------------------------------------------------------------- signals
    def insert_signal(self, s: Signal) -> int:
        return self._insert("signals", s)

    def update_signal(self, s: Signal) -> None:
        self._update("signals", s)

    def recent_signals(self, limit: int = 10, status: str | None = None) -> list[Signal]:
        if status:
            return self._select(Signal, "SELECT * FROM signals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        return self._select(Signal, "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ positions
    def insert_position(self, p: Position) -> int:
        return self._insert("positions", p)

    def update_position(self, p: Position) -> None:
        self._update("positions", p)

    def get_position(self, pid: int) -> Position | None:
        rows = self._select(Position, "SELECT * FROM positions WHERE id=?", (pid,))
        return rows[0] if rows else None

    def open_positions(self, mode: str) -> list[Position]:
        return self._select(Position, "SELECT * FROM positions WHERE status='open' AND mode=? ORDER BY id", (mode,))

    def positions_with_status(self, mode: str, statuses: tuple[str, ...]) -> list[Position]:
        marks = ", ".join("?" * len(statuses))
        return self._select(Position, f"SELECT * FROM positions WHERE mode=? AND status IN ({marks}) ORDER BY id",
                            (mode, *statuses))

    def closed_positions(self, mode: str, since_ms: int = 0) -> list[Position]:
        return self._select(
            Position,
            "SELECT * FROM positions WHERE status='closed' AND mode=? AND closed_at>=? ORDER BY closed_at",
            (mode, since_ms),
        )

    def trade_samples(self) -> pd.DataFrame:
        """Closed trades with their entry features - extra training data for the ML filter."""
        from .ml.features import FEATURE_COLUMNS

        rows = []
        for p in self._select(Position, "SELECT * FROM positions WHERE status='closed'"):
            if not p.features or p.r_multiple is None:
                continue
            r = {c: p.features.get(c) for c in FEATURE_COLUMNS}
            r.update(symbol=p.symbol, timeframe=p.timeframe, strategy=p.strategy,
                     signal_time=pd.Timestamp(p.opened_at, unit="ms", tz="UTC"),
                     exit_time=pd.Timestamp(p.closed_at, unit="ms", tz="UTC"),
                     r_multiple=p.r_multiple, label=int(p.r_multiple > 0))
            rows.append(r)
        return pd.DataFrame(rows)

    # --------------------------------------------------------------- equity
    def record_equity(self, ts: int, mode: str, equity: float) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO equity VALUES (?, ?, ?)", (ts, mode, equity))

    def equity_curve(self, mode: str) -> pd.Series:
        with self._lock:
            rows = self._conn.execute("SELECT ts, equity FROM equity WHERE mode=? ORDER BY ts", (mode,)).fetchall()
        if not rows:
            return pd.Series(dtype=float)
        return pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], unit="ms", utc=True))

    # ------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
