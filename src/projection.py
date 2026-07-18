"""Probabilistic projection + strategy backtest.

Two honest replacements for hand-wavy 'estimated profit':

1. monte_carlo(): instead of a single "if CAGR continued" number, we resample the
   stock's *own historical daily returns* thousands of times to get a RANGE of
   outcomes — median plus a 10th–90th percentile band and the probability of a
   loss. This shows the uncertainty instead of hiding it. It still assumes the
   future resembles the past, which is stated plainly.

2. backtest(): replays a rule over years of price history and reports what
   actually happened next (average forward return, win rate) versus the baseline
   of buying on any random day. This is how you tell whether a rule is real edge
   or noise.
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# --------------------------------------------------------------- monte carlo

def monte_carlo(history: pd.DataFrame, years: float, amount: float,
                sims: int = 3000, lookback_days: int = 3 * TRADING_DAYS,
                seed: int = 12345) -> dict[str, Any] | None:
    """Resample historical daily returns to project `amount` over `years`.
    Returns median / p10 / p90 end values, their returns, and loss probabilities."""
    if history is None or history.empty or "Close" not in history:
        return None
    close = history["Close"].dropna()
    rets = close.pct_change().dropna().to_numpy()
    if len(rets) < 60:
        return None
    rets = rets[-lookback_days:] if len(rets) > lookback_days else rets

    horizon = max(1, int(round(years * TRADING_DAYS)))
    rng = np.random.default_rng(seed)                     # fixed seed = reproducible
    # sample horizon daily returns per sim, compound them
    draws = rng.choice(rets, size=(sims, horizon), replace=True)
    growth = np.prod(1 + draws, axis=1)
    ends = amount * growth

    p10, p50, p90 = np.percentile(ends, [10, 50, 90])
    return {
        "years": years, "amount": amount, "sims": sims,
        "median_end": round(float(p50)), "median_ret": round((p50 / amount - 1) * 100, 1),
        "p10_end": round(float(p10)), "p10_ret": round((p10 / amount - 1) * 100, 1),
        "p90_end": round(float(p90)), "p90_ret": round((p90 / amount - 1) * 100, 1),
        "prob_profit": round(float((ends > amount).mean()) * 100, 0),
        "prob_loss20": round(float((ends < amount * 0.8).mean()) * 100, 0),
    }


# ----------------------------------------------------------------- backtest

def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# preset signals: each takes the close series, returns a boolean Series
def sig_rsi_oversold(close: pd.Series, level: int = 30) -> pd.Series:
    return _rsi_series(close) < level


def sig_dip_below_ma(close: pd.Series, ma: int = 50, pct: float = 0.10) -> pd.Series:
    m = close.rolling(ma).mean()
    return close < m * (1 - pct)


def sig_golden_cross(close: pd.Series) -> pd.Series:
    m50, m200 = close.rolling(50).mean(), close.rolling(200).mean()
    prev = (m50.shift(1) <= m200.shift(1))
    return prev & (m50 > m200)


PRESETS: dict[str, Callable[[pd.Series], pd.Series]] = {
    "RSI oversold (<30)": sig_rsi_oversold,
    "Dip: 10% below 50-day avg": sig_dip_below_ma,
    "Golden cross (50 over 200 DMA)": sig_golden_cross,
}


def backtest(history: pd.DataFrame, signal_name: str,
             horizons=(21, 63, 126, 252)) -> dict[str, Any] | None:
    """Replay a preset signal; report forward returns vs the buy-any-day baseline."""
    if history is None or history.empty or signal_name not in PRESETS:
        return None
    close = history["Close"].dropna()
    if len(close) < TRADING_DAYS + max(horizons):
        return None
    signal = PRESETS[signal_name](close).fillna(False)

    rows = []
    for h in horizons:
        fwd = close.shift(-h) / close - 1
        sig_ret = fwd[signal].dropna()
        base = fwd.dropna()
        label = {21: "1 month", 63: "3 months", 126: "6 months", 252: "1 year"}.get(h, f"{h}d")
        rows.append({
            "horizon": label,
            "avg_return": round(float(sig_ret.mean()) * 100, 1) if len(sig_ret) else None,
            "win_rate": round(float((sig_ret > 0).mean()) * 100) if len(sig_ret) else None,
            "baseline": round(float(base.mean()) * 100, 1) if len(base) else None,
        })
    return {"signal": signal_name, "num_signals": int(signal.sum()),
            "years": round(len(close) / TRADING_DAYS, 1), "results": rows}
