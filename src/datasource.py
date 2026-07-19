"""Free market data for Indian equities.

- History + fundamentals + analyst targets: yfinance (Yahoo).
- Live/intraday quote: nsepython (NSE) with a yfinance fallback.
- Recent news headlines: yfinance.

Everything is wrapped in try/except because these are unofficial/free endpoints
that occasionally rate-limit or change shape. Callers get None / empty rather
than a crash. A short in-process TTL cache avoids hammering them — but crucially
we only cache *successful* results, so a transient failure is retried next call
instead of being frozen (this was the CDSL "all None" bug).
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd
import yfinance as yf

from .config import CONFIG

_CACHE: dict[str, tuple[float, Any]] = {}

_MISS = object()          # sentinel: "this fetch just failed — don't retry for a bit"
_MISS_TTL = 120           # seconds a failure is remembered; keeps dead symbols from
                          # burning retry-sleeps on every dashboard rerun


def _default_ttl() -> int:
    return int(CONFIG["data"]["cache_minutes"]) * 60


def _cached(key: str, ttl: int | None = None):
    """Value if fresh, _MISS if a recent failure, None if absent/expired."""
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if val is _MISS:
        return _MISS if (time.time() - ts) < _MISS_TTL else None
    if (time.time() - ts) < (ttl or _default_ttl()):
        return val
    return None


def _store(key: str, value):
    _CACHE[key] = (time.time(), value)
    return value


def _remember_miss(key: str) -> None:
    _CACHE[key] = (time.time(), _MISS)


def _with_retry(fn, attempts: int = 3, backoff: float = 1.5):
    """Call fn() up to `attempts` times with linear backoff. fn returns a truthy
    result on success or None/empty to signal a retryable miss (rate-limit, gap).
    Returns the successful result, or None if every attempt missed."""
    for i in range(attempts):
        try:
            result = fn()
            if result is not None and not (hasattr(result, "empty") and result.empty):
                return result
        except Exception:
            pass
        if i < attempts - 1:
            time.sleep(backoff * (i + 1))
    return None


def _other_exchange(exchange: str) -> str:
    return "BSE" if exchange.upper() == "NSE" else "NSE"


def yf_symbol(symbol: str, exchange: str = "NSE") -> str:
    """TCS -> TCS.NS (NSE) or TCS.BO (BSE). Pass an already-suffixed symbol through."""
    s = symbol.upper().strip()
    if s.endswith((".NS", ".BO")):
        return s
    return f"{s}.NS" if exchange.upper() == "NSE" else f"{s}.BO"


def _has_suffix(symbol: str) -> bool:
    return symbol.upper().strip().endswith((".NS", ".BO"))


def get_history(symbol: str, exchange: str = "NSE", period: str | None = None) -> pd.DataFrame:
    """OHLCV DataFrame indexed by date. Retries on transient misses, and if the
    chosen exchange has no data, falls back to the other one (e.g. a name added
    as BSE that only Yahoo-lists on NSE). Empty (uncached) DataFrame on failure."""
    period = period or CONFIG["data"]["history_period"]
    key = f"hist:{yf_symbol(symbol, exchange)}:{period}"
    cached = _cached(key)
    if cached is _MISS:
        return pd.DataFrame()          # recent failure — don't hammer/sleep again yet
    if cached is not None:
        return cached

    df = _with_retry(lambda: yf.Ticker(yf_symbol(symbol, exchange)).history(period=period, auto_adjust=True))
    if df is None and not _has_suffix(symbol):
        alt = _other_exchange(exchange)
        df = _with_retry(lambda: yf.Ticker(yf_symbol(symbol, alt)).history(period=period, auto_adjust=True))
    if df is None:
        _remember_miss(key)
        return pd.DataFrame()
    return _store(key, df)


_FUND_FIELDS = [
    "shortName", "longName", "sector", "industry", "currency",
    "trailingPE", "forwardPE", "priceToBook", "pegRatio",
    "returnOnEquity", "returnOnAssets", "debtToEquity", "currentRatio",
    "profitMargins", "operatingMargins", "revenueGrowth", "earningsGrowth",
    "marketCap", "dividendYield", "beta",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage", "twoHundredDayAverage",
    "currentPrice", "regularMarketPrice", "heldPercentInsiders", "heldPercentInstitutions",
    # analyst / recommendation data — real inputs for the Suggestions feature
    "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "recommendationKey", "recommendationMean", "numberOfAnalystOpinions",
]


def get_fundamentals(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Subset of Yahoo's .info we score on + analyst targets. {} (uncached) on failure."""
    key = f"fund:{yf_symbol(symbol, exchange)}"
    cached = _cached(key)
    if cached is _MISS:
        return {}
    if cached is not None:
        return cached

    def fetch(exch):
        try:
            info = yf.Ticker(yf_symbol(symbol, exch)).info or {}
            out = {f: info.get(f) for f in _FUND_FIELDS}
            return out if any(v is not None for v in out.values()) else None
        except Exception:
            return None

    out = _with_retry(lambda: fetch(exchange))
    if out is None and not _has_suffix(symbol):
        out = _with_retry(lambda: fetch(_other_exchange(exchange)))
    if out is None:
        _remember_miss(key)
        return {}
    return _store(key, out)


def get_live_quote(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Latest price + day change. NSE (nsepython) first, yfinance fallback.
    Keys: price, prev_close, change, pct_change, source, ok. Cached ~2 min."""
    key = f"live:{yf_symbol(symbol, exchange)}"
    cached = _cached(key, ttl=120)
    _nores = {"price": None, "prev_close": None, "change": None,
              "pct_change": None, "source": None, "ok": False}
    if cached is _MISS:
        return dict(_nores)
    if cached is not None:
        return cached

    sym = symbol.upper().replace(".NS", "").replace(".BO", "")

    def attempt(exch):
        # NSE live first (real-time) for NSE names
        if exch.upper() == "NSE":
            try:
                from nsepython import nse_eq
                pi = (nse_eq(sym) or {}).get("priceInfo", {})
                price = pi.get("lastPrice")
                if price is not None:
                    return {"price": float(price),
                            "prev_close": float(pi["previousClose"]) if pi.get("previousClose") is not None else None,
                            "change": float(pi["change"]) if pi.get("change") is not None else None,
                            "pct_change": float(pi["pChange"]) if pi.get("pChange") is not None else None,
                            "source": "nse", "ok": True}
            except Exception:
                pass
        # yfinance fallback (delayed but dependable)
        try:
            fi = yf.Ticker(yf_symbol(symbol, exch)).fast_info
            price = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if price is not None:
                change = (price - prev) if prev else None
                pct = (change / prev * 100) if (prev and change is not None) else None
                return {"price": float(price),
                        "prev_close": float(prev) if prev is not None else None,
                        "change": float(change) if change is not None else None,
                        "pct_change": float(pct) if pct is not None else None,
                        "source": "yfinance", "ok": True}
        except Exception:
            pass
        return None

    result = _with_retry(lambda: attempt(exchange), attempts=2)
    if result is None and not _has_suffix(symbol):
        result = _with_retry(lambda: attempt(_other_exchange(exchange)), attempts=2)
    if result is None:
        _remember_miss(key)
        return dict(_nores)
    return _store(key, result)


def get_news(symbol: str, exchange: str = "NSE", limit: int = 6) -> list[dict[str, Any]]:
    """Recent headlines for the stock (real 'current situation' context). [] on failure."""
    key = f"news:{yf_symbol(symbol, exchange)}"
    cached = _cached(key, ttl=1800)
    if cached is _MISS:
        return []
    if cached is not None:
        return cached
    items: list[dict[str, Any]] = []
    try:
        for n in (yf.Ticker(yf_symbol(symbol, exchange)).news or [])[:limit]:
            c = n.get("content", n) if isinstance(n, dict) else {}
            title = c.get("title")
            if not title:
                continue
            provider = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
            items.append({
                "title": title,
                "summary": c.get("summary") or c.get("description") or "",
                "publisher": provider or "",
                "date": (c.get("pubDate") or c.get("displayTime") or "")[:10],
            })
    except Exception:
        items = []
    if not items:
        _remember_miss(key)
        return []
    return _store(key, items)


def resolve_name(symbol: str, exchange: str = "NSE") -> str:
    f = get_fundamentals(symbol, exchange)
    return f.get("longName") or f.get("shortName") or symbol.upper()
