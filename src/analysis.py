"""Metrics + a rules-based, honest scorer.

Design principle: this tool describes a company's financial health and its price
history. It does NOT predict future price or "estimated profit" — nobody can do
that reliably, and pretending to is how people lose money. Every score comes with
the reasons behind it and an explicit disclaimer.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from . import datasource, sectors
from . import fundamentals as fundamentals_mod   # aliased: 'fundamentals' is also a param name below

TRADING_DAYS = 252

DISCLAIMER = (
    "This is a rules-based read of past fundamentals and price history, not "
    "investment advice and not a profit forecast. Past trends do not guarantee "
    "future returns. Verify numbers on the exchange/company filings before acting."
)


# --------------------------------------------------------------------------
# price-derived metrics
# --------------------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0 or pd.isna(last_loss):
        return 100.0
    rs = gain.iloc[-1] / last_loss
    return round(100 - (100 / (1 + rs)), 1)


def _pct(a: float, b: float) -> float | None:
    if b in (0, None) or a is None or pd.isna(b) or pd.isna(a):
        return None
    return round((a / b - 1) * 100, 2)


def compute_metrics(symbol: str, exchange: str = "NSE",
                    history: pd.DataFrame | None = None) -> dict[str, Any]:
    """All the numbers the scorer and alert rules read from. Never raises."""
    df = history if history is not None else datasource.get_history(symbol, exchange)
    m: dict[str, Any] = {"symbol": symbol.upper(), "exchange": exchange.upper()}
    if df is None or df.empty or "Close" not in df:
        return m

    close = df["Close"].dropna()
    if close.empty:
        return m
    price = float(close.iloc[-1])
    m["price"] = round(price, 2)

    # trailing returns
    for label, days in [("ret_1d", 1), ("ret_1w", 5), ("ret_1m", 21),
                        ("ret_3m", 63), ("ret_6m", 126), ("ret_1y", TRADING_DAYS)]:
        if len(close) > days:
            m[label] = _pct(price, float(close.iloc[-days - 1]))

    # moving averages + trend
    if len(close) >= 50:
        m["ma50"] = round(float(close.rolling(50).mean().iloc[-1]), 2)
        m["price_vs_ma50"] = _pct(price, m["ma50"])
    if len(close) >= 200:
        m["ma200"] = round(float(close.rolling(200).mean().iloc[-1]), 2)
        m["price_vs_ma200"] = _pct(price, m["ma200"])

    # 52-week range and where price sits inside it
    window = close.tail(TRADING_DAYS)
    hi, lo = float(window.max()), float(window.min())
    m["high_52w"], m["low_52w"] = round(hi, 2), round(lo, 2)
    if hi > lo:
        m["pos_in_52w_range"] = round((price - lo) / (hi - lo) * 100, 1)  # 0=at low,100=at high
        m["off_52w_high"] = _pct(price, hi)  # negative = below high

    m["rsi14"] = _rsi(close)

    # annualised volatility + CAGR over the fetched window
    rets = close.pct_change().dropna()
    if len(rets) > 30:
        m["volatility_annual"] = round(float(rets.std() * math.sqrt(TRADING_DAYS) * 100), 1)
    if len(close) > TRADING_DAYS:
        years = len(close) / TRADING_DAYS
        cagr = (price / float(close.iloc[0])) ** (1 / years) - 1
        m["cagr"] = round(cagr * 100, 1)
        m["cagr_years"] = round(years, 1)
    return m


# --------------------------------------------------------------------------
# fundamental scoring
# --------------------------------------------------------------------------

def _check(name: str, status: str, value, detail: str) -> dict:
    return {"name": name, "status": status, "value": value, "detail": detail}


def _score_debt_to_equity(de) -> dict | None:
    # Yahoo reports debtToEquity in percent form (e.g. 45.2 == 0.45x).
    if de is None:
        return None
    ratio = de / 100.0
    if de < 50:
        return _check("Debt / Equity", "good", round(ratio, 2), f"{ratio:.2f}x — low leverage")
    if de < 100:
        return _check("Debt / Equity", "ok", round(ratio, 2), f"{ratio:.2f}x — moderate leverage")
    return _check("Debt / Equity", "weak", round(ratio, 2), f"{ratio:.2f}x — high leverage, watch it")


def score_fundamentals(symbol: str, exchange: str = "NSE",
                       fundamentals: dict | None = None,
                       metrics: dict | None = None,
                       deep: bool = False) -> dict[str, Any]:
    """Return {rating, score, checks[], trend, history_context, deep, disclaimer}.

    deep=True also folds statement-based checks (ROCE, FCF margin, earnings quality,
    latest-quarter YoY) into the score — more accurate, but a few more fetches, so
    the overview uses deep=False and the detail/suggestion views use deep=True.
    """
    f = fundamentals if fundamentals is not None else datasource.get_fundamentals(symbol, exchange)
    m = metrics if metrics is not None else compute_metrics(symbol, exchange)
    checks: list[dict] = []

    roe = f.get("returnOnEquity")
    if roe is not None:
        pct = roe * 100
        st = "good" if pct >= 15 else "ok" if pct >= 8 else "weak"
        checks.append(_check("Return on equity", st, round(pct, 1),
                             f"{pct:.1f}% — {'strong' if st=='good' else 'modest' if st=='ok' else 'low'} capital efficiency"))

    de = _score_debt_to_equity(f.get("debtToEquity"))
    if de:
        checks.append(de)

    pm = f.get("profitMargins")
    if pm is not None:
        pct = pm * 100
        st = "good" if pct >= 10 else "ok" if pct >= 3 else "weak"
        checks.append(_check("Net profit margin", st, round(pct, 1),
                             f"{pct:.1f}% net margin"))

    rg = f.get("revenueGrowth")
    if rg is not None:
        pct = rg * 100
        st = "good" if pct >= 10 else "ok" if pct >= 0 else "weak"
        checks.append(_check("Revenue growth (yoy)", st, round(pct, 1),
                             f"{pct:+.1f}% year-on-year"))

    eg = f.get("earningsGrowth")
    if eg is not None:
        pct = eg * 100
        st = "good" if pct >= 10 else "ok" if pct >= 0 else "weak"
        checks.append(_check("Earnings growth (yoy)", st, round(pct, 1),
                             f"{pct:+.1f}% year-on-year"))

    pe = f.get("trailingPE")
    if pe is not None:
        if pe < 0:
            checks.append(_check("Valuation (P/E)", "weak", round(pe, 1),
                                 "negative — company is loss-making on a trailing basis"))
        elif pe > 60:
            checks.append(_check("Valuation (P/E)", "ok", round(pe, 1),
                                 f"{pe:.1f} — richly priced, market expects strong growth"))
        else:
            checks.append(_check("Valuation (P/E)", "good", round(pe, 1),
                                 f"{pe:.1f} — within a normal band (compare to sector)"))

    peg = f.get("pegRatio")
    if peg is not None and peg > 0:
        st = "good" if peg < 1 else "ok" if peg < 2 else "weak"
        checks.append(_check("PEG ratio", st, round(peg, 2),
                             f"{peg:.2f} — {'cheap for its growth' if st=='good' else 'fair' if st=='ok' else 'expensive vs growth'}"))

    # deeper, statement-based checks (only when asked — a few more fetches)
    dm: dict[str, Any] = {}
    if deep:
        dm = fundamentals_mod.deep_metrics(symbol, exchange)
        is_fin = sectors.is_financial(symbol, f)
        roce = dm.get("roce")
        if roce is not None and not is_fin:
            st = "good" if roce >= 20 else "ok" if roce >= 12 else "weak"
            checks.append(_check("Return on capital (ROCE)", st, roce,
                                 f"{roce}% — {'excellent' if st=='good' else 'decent' if st=='ok' else 'low'} return on capital employed"))
        fcfm = dm.get("fcf_margin")
        if fcfm is not None:
            st = "good" if fcfm >= 8 else "ok" if fcfm >= 0 else "weak"
            checks.append(_check("Free cash flow margin", st, fcfm,
                                 f"{fcfm}% of revenue converts to free cash"))
        q = dm.get("ocf_to_ni")
        if q is not None:
            st = "good" if q >= 0.9 else "ok" if q >= 0.7 else "weak"
            checks.append(_check("Earnings quality", st, q,
                                 f"operating cash is {q}x reported profit "
                                 f"({'well' if st=='good' else 'partly' if st=='ok' else 'poorly'} cash-backed)"))
        qe = dm.get("q_earnings_yoy")
        if qe is not None:
            st = "good" if qe >= 10 else "ok" if qe >= 0 else "weak"
            checks.append(_check("Latest quarter profit (YoY)", st, qe,
                                 f"{qe:+}% vs the same quarter last year"))

    # aggregate rating from the pass/fail checks (valuation counts, trend does not)
    weights = {"good": 1.0, "ok": 0.5, "weak": 0.0}
    graded = [c for c in checks if c["status"] in weights]
    rating, score = "Unknown", None
    if graded:
        score = round(sum(weights[c["status"]] for c in graded) / len(graded) * 100)
        rating = "OK" if score >= 65 else "Mixed" if score >= 40 else "Weak"

    # trend read (technical, informational — not part of the health score)
    trend = {}
    if m.get("price_vs_ma50") is not None and m.get("price_vs_ma200") is not None:
        above50 = m["price_vs_ma50"] > 0
        above200 = m["price_vs_ma200"] > 0
        if above50 and above200:
            trend["direction"] = "uptrend (above 50 & 200 DMA)"
        elif not above50 and not above200:
            trend["direction"] = "downtrend (below 50 & 200 DMA)"
        else:
            trend["direction"] = "sideways / transitioning"
    if m.get("rsi14") is not None:
        r = m["rsi14"]
        trend["rsi"] = f"{r} ({'overbought' if r > 70 else 'oversold' if r < 30 else 'neutral'})"

    # honest historical context — described, never projected forward
    hist = {}
    if m.get("cagr") is not None:
        hist["cagr"] = f"{m['cagr']}% per year over the last ~{m['cagr_years']}y"
    if m.get("pos_in_52w_range") is not None:
        hist["range_position"] = (
            f"{m['pos_in_52w_range']}% up its 52-week range "
            f"(low {m.get('low_52w')} — high {m.get('high_52w')})"
        )
    if m.get("volatility_annual") is not None:
        hist["volatility"] = f"{m['volatility_annual']}% annualised (higher = bigger swings)"

    return {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "name": f.get("longName") or f.get("shortName") or symbol.upper(),
        "sector": f.get("sector"),
        "rating": rating,
        "score": score,
        "checks": checks,
        "trend": trend,
        "history_context": hist,
        "deep": dm,
        "disclaimer": DISCLAIMER,
    }
