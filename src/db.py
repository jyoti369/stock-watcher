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
    last_triggered   TEXT,
    mode             TEXT NOT NULL DEFAULT 'level',  -- 'level' = while true, 'edge' = on false->true
    last_state       INTEGER           -- last evaluation (1/0), for edge detection
);

CREATE TABLE IF NOT EXISTS holdings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL DEFAULT 'NSE',
    qty        REAL NOT NULL,
    buy_price  REAL NOT NULL,
    buy_date   TEXT,
    added_at   TEXT NOT NULL
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
        # migrate older DBs that predate the edge-alert columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_rules)").fetchall()}
        if "mode" not in cols:
            conn.execute("ALTER TABLE alert_rules ADD COLUMN mode TEXT NOT NULL DEFAULT 'level'")
        if "last_state" not in cols:
            conn.execute("ALTER TABLE alert_rules ADD COLUMN last_state INTEGER")


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

def add_rule(symbol: str, exchange: str, label: str, conditions: list[dict],
             mode: str = "level") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO alert_rules(symbol, exchange, label, conditions, active, created_at, mode) "
            "VALUES (?,?,?,?,1,?,?)",
            (symbol.upper(), exchange.upper(), label, json.dumps(conditions), now_iso(), mode),
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


def set_last_state(rule_id: int, state: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE alert_rules SET last_state=? WHERE id=?", (1 if state else 0, rule_id))


def set_last_triggered(rule_id: int, iso: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE alert_rules SET last_triggered=? WHERE id=?", (iso, rule_id))


# ---- holdings (portfolio) -------------------------------------------------

def add_holding(symbol: str, exchange: str, qty: float, buy_price: float,
                buy_date: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO holdings(symbol, exchange, qty, buy_price, buy_date, added_at) "
            "VALUES (?,?,?,?,?,?)",
            (symbol.upper(), exchange.upper(), qty, buy_price, buy_date, now_iso()),
        )
        return cur.lastrowid


def get_holdings() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM holdings ORDER BY symbol, buy_date").fetchall()
    return [dict(r) for r in rows]


def remove_holding(holding_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM holdings WHERE id=?", (holding_id,))


def replace_holdings(rows: Iterable[dict]) -> int:
    """Broker-import semantics: the imported statement is the whole truth."""
    rows = list(rows)
    with connect() as conn:
        conn.execute("DELETE FROM holdings")
        for r in rows:
            conn.execute(
                "INSERT INTO holdings(symbol, exchange, qty, buy_price, buy_date, added_at) "
                "VALUES (?,?,?,?,?,?)",
                (r["symbol"].upper(), r.get("exchange", "NSE").upper(), r["qty"],
                 r["buy_price"], r.get("buy_date"), now_iso()),
            )
    return len(rows)


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
