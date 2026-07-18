"""Deeper, statement-based fundamentals.

Yahoo's `.info` gives shallow summary ratios that are often stale. The financial
*statements* (income, balance sheet, cash flow — 5 annual years + recent quarters)
are more reliable, so we compute the important ratios ourselves from them:

    ROCE, free cash flow + FCF margin, earnings quality (OCF / net income),
    net-margin trend, revenue/EPS CAGR, debt trend + interest coverage,
    latest-quarter YoY growth, and an annual EPS history (for PE-band work).

Every field degrades to None independently — banks/financials and small caps
often omit some line items, and that must not break the rest.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd
import yfinance as yf

from .datasource import yf_symbol

_CACHE: dict[str, tuple[float, Any]] = {}
_TTL = 6 * 3600   # statements change quarterly; cache generously


def _row(df: pd.DataFrame | None, *names: str) -> pd.Series | None:
    """First matching statement row, as a Series (columns = dates, newest first)."""
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = df.loc[n]
            # a duplicated index can hand back a DataFrame — take the first row
            return s.iloc[0] if isinstance(s, pd.DataFrame) else s
    return None


def _latest(series: pd.Series | None):
    if series is None:
        return None
    s = series.dropna()
    return float(s.iloc[0]) if len(s) else None


def _at(series: pd.Series | None, i: int):
    if series is None:
        return None
    s = series.dropna()
    return float(s.iloc[i]) if len(s) > i else None


def _cagr(series: pd.Series | None) -> float | None:
    """CAGR from oldest to newest non-null value in a statement row."""
    if series is None:
        return None
    s = series.dropna()
    if len(s) < 2:
        return None
    new, old = float(s.iloc[0]), float(s.iloc[-1])       # newest first
    yrs = len(s) - 1
    if old <= 0 or new <= 0:
        return None
    return round(((new / old) ** (1 / yrs) - 1) * 100, 1)


def _statements(symbol: str, exchange: str):
    key = f"stmt:{yf_symbol(symbol, exchange)}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    out = {"inc": None, "qinc": None, "bs": None, "cf": None}
    try:
        t = yf.Ticker(yf_symbol(symbol, exchange))
        out = {"inc": t.income_stmt, "qinc": t.quarterly_income_stmt,
               "bs": t.balance_sheet, "cf": t.cashflow}
    except Exception:
        pass
    if all(v is None or getattr(v, "empty", True) for v in out.values()):
        return out                              # don't cache a total failure
    _CACHE[key] = (time.time(), out)
    return out


def deep_metrics(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Statement-derived metrics. Keys are absent when the data isn't there."""
    st = _statements(symbol, exchange)
    inc, qinc, bs, cf = st["inc"], st["qinc"], st["bs"], st["cf"]
    m: dict[str, Any] = {}

    rev = _row(inc, "Total Revenue", "Operating Revenue")
    ni = _row(inc, "Net Income", "Net Income Common Stockholders")
    ebit = _row(inc, "EBIT", "Operating Income")
    eps = _row(inc, "Diluted EPS", "Basic EPS")
    interest = _row(inc, "Interest Expense", "Interest Expense Non Operating")

    assets = _row(bs, "Total Assets")
    cur_liab = _row(bs, "Current Liabilities")
    debt = _row(bs, "Total Debt")
    equity = _row(bs, "Stockholders Equity", "Common Stock Equity")

    ocf = _row(cf, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    fcf = _row(cf, "Free Cash Flow")

    # ROCE = EBIT / capital employed (assets - current liabilities)
    ebit_l, assets_l, cl_l = _latest(ebit), _latest(assets), _latest(cur_liab)
    if ebit_l is not None and assets_l and cl_l is not None and (assets_l - cl_l) > 0:
        m["roce"] = round(ebit_l / (assets_l - cl_l) * 100, 1)

    # free cash flow + margin, and earnings quality (OCF vs net income)
    rev_l, ni_l = _latest(rev), _latest(ni)
    fcf_l, ocf_l = _latest(fcf), _latest(ocf)
    if fcf_l is not None:
        m["fcf"] = round(fcf_l, 0)
        if rev_l:
            m["fcf_margin"] = round(fcf_l / rev_l * 100, 1)
    if ocf_l is not None and ni_l:
        m["ocf_to_ni"] = round(ocf_l / ni_l, 2)          # ~1 healthy, <0.7 = watch quality

    # margin now vs a year ago (trend)
    if rev_l and ni_l is not None:
        m["net_margin"] = round(ni_l / rev_l * 100, 1)
        ni_p, rev_p = _at(ni, 1), _at(rev, 1)
        if ni_p is not None and rev_p:
            m["net_margin_prev"] = round(ni_p / rev_p * 100, 1)
            m["net_margin_delta"] = round(m["net_margin"] - m["net_margin_prev"], 1)

    # multi-year growth
    m["revenue_cagr"] = _cagr(rev)
    m["eps_cagr"] = _cagr(eps)

    # debt trend + leverage + interest cover
    debt_l, debt_o, eq_l = _latest(debt), (None if debt is None else _at(debt, min(3, len(debt.dropna()) - 1) if len(debt.dropna()) else 0)), _latest(equity)
    if debt_l is not None:
        m["total_debt"] = round(debt_l, 0)
        if eq_l:
            m["debt_to_equity"] = round(debt_l / eq_l, 2)
        d_old = _at(debt, len(debt.dropna()) - 1)
        if d_old is not None:
            m["debt_trend"] = "rising" if debt_l > d_old * 1.1 else "falling" if debt_l < d_old * 0.9 else "stable"
    if ebit_l is not None and _latest(interest):
        ic = ebit_l / abs(_latest(interest))
        m["interest_cover"] = round(ic, 1)

    # latest reported quarter, YoY (quarter vs same quarter last year = 4 cols back)
    q_rev, q_ni = _row(qinc, "Total Revenue", "Operating Revenue"), _row(qinc, "Net Income", "Net Income Common Stockholders")
    if q_rev is not None:
        cur, yr_ago = _at(q_rev, 0), _at(q_rev, 4)
        if cur is not None and yr_ago:
            m["q_revenue_yoy"] = round((cur / yr_ago - 1) * 100, 1)
    if q_ni is not None:
        cur, yr_ago = _at(q_ni, 0), _at(q_ni, 4)
        if cur is not None and yr_ago and yr_ago > 0:
            m["q_earnings_yoy"] = round((cur / yr_ago - 1) * 100, 1)

    # annual EPS history (date -> eps) for the PE-band / valuation-percentile work
    if eps is not None:
        s = eps.dropna()
        m["eps_history"] = {str(d)[:10]: round(float(v), 2) for d, v in s.items()}

    return m
