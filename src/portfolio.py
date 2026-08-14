"""Portfolio P&L math, kept pure so it's unit-testable.

The dashboard passes each holding plus the live values it already fetched;
this computes per-lot and total P&L without touching the network.
"""
from __future__ import annotations

from typing import Any


def lot_row(holding: dict, values: dict) -> dict[str, Any]:
    """One holding (lot) -> P&L numbers. `values` is watcher.gather_values output."""
    price = values.get("price")
    invested = holding["qty"] * holding["buy_price"]
    value = holding["qty"] * price if price is not None else None
    pnl = (value - invested) if value is not None else None
    return {
        "symbol": holding["symbol"],
        "qty": holding["qty"],
        "buy_price": holding["buy_price"],
        "price": price,
        "day_pct": values.get("pct_change_day"),
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": (pnl / invested * 100) if (pnl is not None and invested) else None,
    }


def by_symbol(rows: list[dict]) -> list[dict[str, Any]]:
    """Roll lot rows up into one position per symbol, biggest holding first.

    Three separate SUZLON buys are one position when you're reading a digest,
    so the buy price shown is the quantity-weighted average of the lots.
    """
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = merged.setdefault(r["symbol"], {
            "symbol": r["symbol"], "qty": 0.0, "invested": 0.0, "value": 0.0,
            "price": r.get("price"), "day_pct": r.get("day_pct"),
            "priced": False,
        })
        p["qty"] += r["qty"]
        p["invested"] += r["invested"]
        if r.get("value") is not None:
            p["value"] += r["value"]
            p["priced"] = True
            p["price"], p["day_pct"] = r.get("price"), r.get("day_pct")
    out = []
    for p in merged.values():
        if not p.pop("priced"):
            p["value"] = None
        p["buy_price"] = p["invested"] / p["qty"] if p["qty"] else None
        p["pnl"] = (p["value"] - p["invested"]) if p["value"] is not None else None
        p["pnl_pct"] = (p["pnl"] / p["invested"] * 100) \
            if (p["pnl"] is not None and p["invested"]) else None
        out.append(p)
    out.sort(key=lambda p: -(p["value"] if p["value"] is not None else p["invested"]))
    return out


def totals(rows: list[dict]) -> dict[str, Any]:
    """Aggregate the lot rows. Day-move is derived from each lot's day % so the
    'today' figure is in rupees, not an average of percentages."""
    invested = sum(r["invested"] for r in rows)
    valued = [r for r in rows if r["value"] is not None]
    value = sum(r["value"] for r in valued)
    day_move = 0.0
    for r in valued:
        if r["day_pct"] is not None:
            day_move += r["value"] - r["value"] / (1 + r["day_pct"] / 100)
    pnl = value - sum(r["invested"] for r in valued)
    return {
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": (pnl / invested * 100) if invested else None,
        "day_move": day_move,
        "day_pct": (day_move / value * 100) if value else None,
        "missing": len(rows) - len(valued),   # lots with no live price this run
    }
