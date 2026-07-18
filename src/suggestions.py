"""Suggestion engine.

Two-phase so it stays fast and doesn't hammer the free APIs:
  1. rank(): score the whole universe on LIGHT data (health from summary ratios,
     analyst upside, trend, momentum) and sort.
  2. enrich(): only for the top N, pull the DEEP data — statement-based health,
     the full bear case, and the peer comparison — for the detail cards.

Profit numbers in the UI come from projection.monte_carlo() (a probability range),
never a single made-up figure. Analyst targets are shown as the market's own view.
"""
from __future__ import annotations

from typing import Any

from . import analysis, datasource

# A shortlist of liquid NSE names so Suggestions is useful even with a thin
# watchlist. Not a recommendation in itself — just the scan universe.
DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "LT",
    "BHARTIARTL", "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "HCLTECH",
]

REC_LABEL = {
    "strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
    "underperform": "Underperform", "sell": "Sell", "none": "No coverage",
}


def analyst_view(fund: dict, price: float | None) -> dict | None:
    """Consensus target + implied upside. None if analysts don't cover it."""
    target = fund.get("targetMeanPrice")
    if not target or not price:
        return None
    return {
        "target": round(float(target), 2),
        "upside_pct": round((target / price - 1) * 100, 1),
        "low": fund.get("targetLowPrice"),
        "high": fund.get("targetHighPrice"),
        "recommendation": REC_LABEL.get(fund.get("recommendationKey"), fund.get("recommendationKey") or "—"),
        "num_analysts": fund.get("numberOfAnalystOpinions"),
    }


def opportunity_score(symbol: str, exchange: str = "NSE", deep: bool = False) -> dict[str, Any] | None:
    """Composite 0-100 opportunity score + the pieces behind it. None if no data."""
    metrics = analysis.compute_metrics(symbol, exchange)
    fund = datasource.get_fundamentals(symbol, exchange)
    if not metrics.get("price") and not fund:
        return None
    health = analysis.score_fundamentals(symbol, exchange, fundamentals=fund, metrics=metrics, deep=deep)
    price = metrics.get("price") or fund.get("currentPrice")
    av = analyst_view(fund, price)

    total, wsum, comps = 0.0, 0.0, {}
    if health.get("score") is not None:                      # fundamental health (40%)
        comps["Fundamental health"] = health["score"]
        total += health["score"] * 0.40; wsum += 0.40
    if av:                                                    # analyst upside (30%)
        up = max(min(av["upside_pct"], 40), -20)
        norm = round((up + 20) / 60 * 100)
        comps["Analyst upside"] = norm
        total += norm * 0.30; wsum += 0.30
    if metrics.get("price_vs_ma200") is not None:            # trend (20%)
        tr = 100 if metrics["price_vs_ma200"] > 0 else 45
        comps["Trend (vs 200-DMA)"] = tr
        total += tr * 0.20; wsum += 0.20
    if metrics.get("rsi14") is not None:                     # room to run (10%)
        r = metrics["rsi14"]
        room = 100 if r < 60 else 60 if r < 70 else 25
        comps["Not overbought"] = room
        total += room * 0.10; wsum += 0.10

    return {
        "symbol": symbol.upper(), "exchange": exchange.upper(),
        "name": health.get("name", symbol.upper()), "sector": health.get("sector"),
        "score": round(total / wsum) if wsum else None,
        "components": comps, "price": price,
        "health": health, "metrics": metrics, "fund": fund, "analyst": av,
    }


def enrich(row: dict) -> dict:
    """Add the deep data used only for the top-N detail cards."""
    from . import bearcase, sectors
    sym, exch = row["symbol"], row["exchange"]
    row["health"] = analysis.score_fundamentals(sym, exch, fundamentals=row["fund"],
                                                 metrics=row["metrics"], deep=True)
    row["bear"] = bearcase.bear_case(sym, exch)
    row["peer"] = sectors.peer_comparison(sym, exch, row["fund"])
    return row


def rank(universe: list[str], exchange: str = "NSE", top_n: int = 5) -> list[dict[str, Any]]:
    """Light-score the whole universe, sort, then deep-enrich only the top_n."""
    scored, seen = [], set()
    for s in universe:
        s = s.upper()
        if s in seen:
            continue
        seen.add(s)
        try:
            r = opportunity_score(s, exchange, deep=False)
        except Exception:
            r = None
        if r and r.get("score") is not None:
            scored.append(r)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return [enrich(r) for r in scored[:top_n]]
