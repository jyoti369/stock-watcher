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
        "atp": values.get("atp"),
        "day_high": values.get("day_high"),
        "day_low": values.get("day_low"),
        "day_pct": values.get("pct_change_day"),
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": (pnl / invested * 100) if (pnl is not None and invested) else None,
    }


_SORTS = {
    "value": lambda p: -(p["value"] if p["value"] is not None else p["invested"]),
    "pnl": lambda p: -abs(p["pnl"]) if p["pnl"] is not None else 1,
    "pnl_pct": lambda p: -abs(p["pnl_pct"]) if p["pnl_pct"] is not None else 1,
    "day": lambda p: -abs(p["day_pct"]) if p.get("day_pct") is not None else 1,
}


def by_symbol(rows: list[dict], sort: str = "value") -> list[dict[str, Any]]:
    """Roll lot rows up into one position per symbol.

    Three separate SUZLON buys are one position when you're reading a digest,
    so the buy price shown is the quantity-weighted average of the lots.

    `sort`: value = biggest holding first · pnl = biggest rupee move first ·
    pnl_pct = biggest percentage move first · day = today's movers first. The
    P&L orders use the absolute size, so the worst loss and the best gain both
    surface instead of one end of the list being buried.
    """
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = merged.setdefault(r["symbol"], {
            "symbol": r["symbol"], "qty": 0.0, "invested": 0.0, "value": 0.0,
            "price": r.get("price"), "day_pct": r.get("day_pct"),
            "atp": r.get("atp"), "day_high": r.get("day_high"),
            "day_low": r.get("day_low"), "priced": False,
        })
        p["qty"] += r["qty"]
        p["invested"] += r["invested"]
        if r.get("value") is not None:
            p["value"] += r["value"]
            p["priced"] = True
            p["price"], p["day_pct"] = r.get("price"), r.get("day_pct")
            for k in ("atp", "day_high", "day_low"):
                p[k] = r.get(k)
    out = []
    for p in merged.values():
        if not p.pop("priced"):
            p["value"] = None
        p["buy_price"] = p["invested"] / p["qty"] if p["qty"] else None
        p["pnl"] = (p["value"] - p["invested"]) if p["value"] is not None else None
        p["pnl_pct"] = (p["pnl"] / p["invested"] * 100) \
            if (p["pnl"] is not None and p["invested"]) else None
        out.append(p)
    out.sort(key=_SORTS.get(sort, _SORTS["value"]))
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
