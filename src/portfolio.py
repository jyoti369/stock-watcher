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
