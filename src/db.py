"""SQLite persistence: watchlist, price snapshots, alert rules and alert history.

One local file (data/stocks.db). No server, no setup. Everything the watcher and
dashboard share lives here.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL DEFAULT 'NSE',
    name       TEXT,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, exchange)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    exchange  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    price     REAL,
    metrics   TEXT              -- JSON blob of computed metrics at snapshot time
);
CREATE INDEX IF NOT EXISTS idx_snap_symbol ON snapshots(symbol, exchange, ts);

CREATE TABLE IF NOT EXISTS alert_rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    exchange         TEXT NOT NULL DEFAULT 'NSE',
    label            TEXT,             -- human name for the rule
    conditions       TEXT NOT NULL,    -- JSON list of {metric, op, value}, ANDed together
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_triggered   TEXT
);

CREATE TABLE IF NOT EXISTS alert_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    INTEGER,
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    message    TEXT NOT NULL,
    channels   TEXT              -- JSON list of channels the alert went to
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---- watchlist -----------------------------------------------------------

def add_to_watchlist(symbol: str, exchange: str = "NSE", name: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist(symbol, exchange, name, added_at) VALUES (?,?,?,?)",
            (symbol.upper(), exchange.upper(), name, now_iso()),
        )


def remove_from_watchlist(symbol: str, exchange: str = "NSE") -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE symbol=? AND exchange=?",
            (symbol.upper(), exchange.upper()),
        )


def get_watchlist() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


# ---- snapshots -----------------------------------------------------------

def save_snapshot(symbol: str, exchange: str, price: float | None, metrics: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO snapshots(symbol, exchange, ts, price, metrics) VALUES (?,?,?,?,?)",
            (symbol.upper(), exchange.upper(), now_iso(), price, json.dumps(metrics)),
        )


# ---- alert rules ---------------------------------------------------------

def add_rule(symbol: str, exchange: str, label: str, conditions: list[dict]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO alert_rules(symbol, exchange, label, conditions, active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (symbol.upper(), exchange.upper(), label, json.dumps(conditions), now_iso()),
        )
        return cur.lastrowid


def get_rules(active_only: bool = True) -> list[dict[str, Any]]:
    q = "SELECT * FROM alert_rules"
    if active_only:
        q += " WHERE active=1"
    with connect() as conn:
        rows = conn.execute(q).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["conditions"] = json.loads(d["conditions"])
        out.append(d)
    return out


def set_rule_active(rule_id: int, active: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE alert_rules SET active=? WHERE id=?", (1 if active else 0, rule_id))


def delete_rule(rule_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))


def mark_triggered(rule_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE alert_rules SET last_triggered=? WHERE id=?", (now_iso(), rule_id))


# ---- alert history -------------------------------------------------------

def log_alert(rule_id: int | None, symbol: str, exchange: str, message: str,
              channels: Iterable[str]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO alert_history(rule_id, symbol, exchange, ts, message, channels) "
            "VALUES (?,?,?,?,?,?)",
            (rule_id, symbol.upper(), exchange.upper(), now_iso(), message,
             json.dumps(list(channels))),
        )


def get_alert_history(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"initialised db at {DB_PATH}")
