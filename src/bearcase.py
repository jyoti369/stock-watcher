"""Bear-case / risk intelligence — the honest 'why NOT' for a stock.

Three sources of red flags, all from real data:
  - valuation_percentile(): is today's P/E near the expensive end of the stock's
    OWN 5-year range? (built from reported annual EPS + price history)
  - earnings-quality flags from the statement metrics (cash backing, margin trend,
    latest-quarter YoY, debt trend)
  - trend + peer context

Financials (banks/NBFCs) skip the debt/interest-cover flags, which don't apply.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import analysis, datasource, fundamentals, sectors


def valuation_percentile(symbol: str, exchange: str = "NSE",
                         history: pd.DataFrame | None = None,
                         deep: dict | None = None) -> dict[str, Any] | None:
    """Where today's P/E sits within its own ~5y range. 100 = most expensive ever."""
    deep = deep if deep is not None else fundamentals.deep_metrics(symbol, exchange)
    eps_hist = deep.get("eps_history")
    if not eps_hist:
        return None
    hist = history if history is not None else datasource.get_history(symbol, exchange, period="5y")
    if hist is None or hist.empty:
        return None

    close = hist["Close"].dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)   # drop tz for alignment
    eps = pd.Series({pd.Timestamp(d): v for d, v in eps_hist.items()}).sort_index()
    eps = eps[eps > 0]
    if eps.empty:
        return None

    # step the most-recent reported annual EPS forward across the price history
    eps_aligned = eps.reindex(close.index.union(eps.index)).sort_index().ffill().reindex(close.index)
    pe = (close / eps_aligned).replace([np.inf, -np.inf], np.nan).dropna()
    pe = pe[pe > 0]
    if len(pe) < 60:
        return None

    current = float(pe.iloc[-1])
    pctile = round(float((pe <= current).mean()) * 100)
    verdict = ("cheap end of its own range" if pctile <= 30
               else "expensive end of its own range" if pctile >= 70
               else "mid of its own range")
    return {"current_pe": round(current, 1), "median_pe": round(float(pe.median()), 1),
            "min_pe": round(float(pe.min()), 1), "max_pe": round(float(pe.max()), 1),
            "percentile": pctile, "verdict": verdict}


def earnings_quality(deep: dict, is_financial: bool) -> list[str]:
    flags = []
    q = deep.get("ocf_to_ni")
    if q is not None and q < 0.75:
        flags.append(f"Earnings only {q}x backed by operating cash — accrual quality worth checking.")
    d = deep.get("net_margin_delta")
    if d is not None and d < -3:
        flags.append(f"Margins compressed {deep.get('net_margin_prev')}% → {deep.get('net_margin')}% YoY.")
    qe = deep.get("q_earnings_yoy")
    if qe is not None and qe < 0:
        flags.append(f"Latest quarter profit down {qe}% YoY.")
    qr = deep.get("q_revenue_yoy")
    if qr is not None and qr < 0:
        flags.append(f"Latest quarter revenue down {qr}% YoY.")
    rc = deep.get("revenue_cagr")
    if rc is not None and rc < 3:
        flags.append(f"Revenue nearly flat ({rc}%/yr over ~5y).")
    if not is_financial:
        if deep.get("debt_trend") == "rising" and (deep.get("debt_to_equity") or 0) > 1:
            flags.append(f"Debt rising with D/E at {deep.get('debt_to_equity')}x.")
        ic = deep.get("interest_cover")
        if ic is not None and ic < 3:
            flags.append(f"Thin interest cover ({ic}x) — vulnerable if earnings dip.")
    return flags


def bear_case(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Consolidated red flags for the 'why NOT buy' view."""
    fund = datasource.get_fundamentals(symbol, exchange)
    deep = fundamentals.deep_metrics(symbol, exchange)
    metrics = analysis.compute_metrics(symbol, exchange)
    is_fin = sectors.is_financial(symbol, fund)

    flags = list(earnings_quality(deep, is_fin))

    val = valuation_percentile(symbol, exchange, deep=deep)
    if val and val["percentile"] >= 70:
        flags.append(f"P/E {val['current_pe']} is in the {val['percentile']}th percentile of its "
                     f"own 5y range (median {val['median_pe']}) — pricey vs its history.")

    peer = sectors.peer_comparison(symbol, exchange, fund)
    if peer and peer["verdict"].get("pe") == "pricier than peers":
        flags.append(f"Valued richer than {peer['group']} peers "
                     f"(P/E {peer['me'].get('pe')} vs median {peer['medians'].get('pe')}).")

    if metrics.get("price_vs_ma200") is not None and metrics["price_vs_ma200"] < -5:
        flags.append(f"Trading {abs(metrics['price_vs_ma200'])}% below its 200-day average — "
                     "longer-term trend still down.")

    pe = fund.get("trailingPE")
    if pe and pe > 60:
        flags.append(f"High absolute P/E ({pe:.0f}) — a lot of future growth is already priced in.")

    if not flags:
        flags.append("No standout red flags in the data — but every stock carries market risk.")
    return {"symbol": symbol.upper(), "is_financial": is_fin,
            "flags": flags, "valuation": val, "peer": peer}
