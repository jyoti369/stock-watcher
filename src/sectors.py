"""Sector-relative valuation. A raw 'P/E 64' means nothing on its own — it only
matters against the stock's own peer group. We keep curated peer lists for the
liquid parts of the Indian market and compare a stock to its group's medians.
"""
from __future__ import annotations

import statistics
from typing import Any

from . import datasource

# Curated peer groups (liquid NSE names). Not exhaustive — just enough that the
# common stocks land in a sensible bucket. A stock not listed here falls back to
# Yahoo's sector label with no peer table.
PEER_GROUPS: dict[str, list[str]] = {
    "IT Services": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM"],
    "Private Banks": ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "INDUSINDBK"],
    "PSU Banks": ["SBIN", "BANKBARODA", "PNB", "CANBK"],
    "NBFC / Finance": ["BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "SBICARD", "MUTHOOTFIN"],
    "Capital Markets": ["CDSL", "BSE", "MCX", "CAMS", "KFINTECH", "ANGELONE"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO"],
    "Auto": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA"],
    "Energy / Power": ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "COALINDIA", "IOC"],
    "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL"],
    "Cement": ["ULTRACEMCO", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT"],
    "Paints": ["ASIANPAINT", "BERGEPAINT", "KANSAINER"],
    "Telecom": ["BHARTIARTL", "IDEA", "INDUSTOWER"],
    "Consumer / Retail": ["TITAN", "TRENT", "DMART", "PAGEIND", "JUBLFOOD"],
}

_SYMBOL_TO_GROUP = {s: g for g, syms in PEER_GROUPS.items() for s in syms}

FINANCIAL_GROUPS = {"Private Banks", "PSU Banks", "NBFC / Finance", "Capital Markets"}


def group_of(symbol: str, fundamentals: dict | None = None) -> str | None:
    g = _SYMBOL_TO_GROUP.get(symbol.upper())
    if g:
        return g
    # fall back to Yahoo's sector label (no curated peers, but still labelled)
    if fundamentals and fundamentals.get("sector"):
        return fundamentals["sector"]
    return None


def is_financial(symbol: str, fundamentals: dict | None = None) -> bool:
    g = _SYMBOL_TO_GROUP.get(symbol.upper())
    if g in FINANCIAL_GROUPS:
        return True
    sec = (fundamentals or {}).get("sector") or ""
    return "financ" in sec.lower() or "bank" in sec.lower()


def _peer_row(sym: str) -> dict | None:
    f = datasource.get_fundamentals(sym)
    if not f:
        return None
    pe = f.get("trailingPE")
    roe = f.get("returnOnEquity")
    nm = f.get("profitMargins")
    rg = f.get("revenueGrowth")
    return {
        "symbol": sym,
        "pe": round(pe, 1) if pe else None,
        "roe": round(roe * 100, 1) if roe is not None else None,
        "net_margin": round(nm * 100, 1) if nm is not None else None,
        "rev_growth": round(rg * 100, 1) if rg is not None else None,
    }


def _median(vals: list) -> float | None:
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(statistics.median(v), 1) if v else None


def peer_comparison(symbol: str, exchange: str = "NSE",
                    fundamentals: dict | None = None) -> dict[str, Any] | None:
    """Compare a stock's PE/ROE/margin/growth to its curated peer group's medians."""
    symbol = symbol.upper()
    group = _SYMBOL_TO_GROUP.get(symbol)
    if not group:
        return None                              # no curated peers → nothing to compare

    peers = [_peer_row(s) for s in PEER_GROUPS[group]]
    peers = [p for p in peers if p]
    if len(peers) < 2:
        return None

    medians = {k: _median([p[k] for p in peers]) for k in ("pe", "roe", "net_margin", "rev_growth")}
    me = next((p for p in peers if p["symbol"] == symbol), None) or _peer_row(symbol)

    verdict = {}
    if me and me.get("pe") and medians.get("pe"):
        ratio = me["pe"] / medians["pe"]
        verdict["pe"] = ("cheaper than peers" if ratio < 0.85
                         else "pricier than peers" if ratio > 1.15 else "in line with peers")
    if me and me.get("roe") is not None and medians.get("roe") is not None:
        verdict["roe"] = ("more profitable than peers" if me["roe"] > medians["roe"] + 2
                          else "less profitable than peers" if me["roe"] < medians["roe"] - 2
                          else "similar profitability to peers")

    return {"group": group, "me": me, "peers": peers, "medians": medians, "verdict": verdict}
