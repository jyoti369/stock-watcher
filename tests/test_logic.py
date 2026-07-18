"""Offline unit tests for the pure logic — no network. Run: pytest -q

These lock in the maths (metrics, scoring, rule evaluation, projection) so future
tweaks can't silently change the numbers the app reports.
"""
import numpy as np
import pandas as pd

from src import analysis, fundamentals, projection, repo_state, sectors, suggestions, watcher


def _price_df(prices):
    idx = pd.date_range("2019-01-01", periods=len(prices), freq="D")
    p = list(prices)
    return pd.DataFrame({"Open": p, "High": p, "Low": p, "Close": p, "Volume": [1] * len(p)}, index=idx)


# ---- analysis.compute_metrics -------------------------------------------

def test_metrics_on_rising_series():
    m = analysis.compute_metrics("X", "NSE", history=_price_df(range(100, 400)))
    assert m["price"] == 399.0
    assert m["ret_1d"] > 0
    assert m["price_vs_ma50"] > 0 and m["price_vs_ma200"] > 0   # price above averages
    assert 0 <= m["rsi14"] <= 100 and m["rsi14"] > 70           # monotonic up = overbought


def test_52w_range_position():
    m = analysis.compute_metrics("X", "NSE", history=_price_df(range(100, 460)))
    assert m["low_52w"] < m["price"] <= m["high_52w"]
    assert 90 <= m["pos_in_52w_range"] <= 100                   # near the top of its range


# ---- analysis.score_fundamentals (offline via injected data) ------------

def test_strong_fundamentals_score_high():
    fund = {"longName": "T", "returnOnEquity": 0.25, "debtToEquity": 20,
            "profitMargins": 0.15, "revenueGrowth": 0.2, "earningsGrowth": 0.2, "trailingPE": 20}
    metrics = {"price": 100, "price_vs_ma50": 5, "price_vs_ma200": 10, "rsi14": 55}
    s = analysis.score_fundamentals("X", "NSE", fundamentals=fund, metrics=metrics)
    assert s["rating"] == "OK" and s["score"] >= 65


def test_weak_fundamentals_score_low():
    fund = {"returnOnEquity": 0.02, "debtToEquity": 250, "profitMargins": -0.05,
            "revenueGrowth": -0.1, "earningsGrowth": -0.2, "trailingPE": -5}
    s = analysis.score_fundamentals("X", "NSE", fundamentals=fund, metrics={"price": 10})
    assert s["rating"] == "Weak" and s["score"] < 40


# ---- watcher rule evaluation -------------------------------------------

def test_rule_true_false_and_gap():
    rule = {"conditions": [{"metric": "price", "op": "<", "value": 50}]}
    assert watcher.evaluate_rule(rule, {"price": 40}) == (True, ["Last price (₹): 40 < 50"], True)
    fired, _, ok = watcher.evaluate_rule(rule, {"price": 60}); assert not fired and ok
    fired, _, ok = watcher.evaluate_rule(rule, {"price": None}); assert not fired and not ok


def test_rule_and_semantics():
    rule = {"conditions": [{"metric": "pe", "op": "<", "value": 25},
                           {"metric": "rsi14", "op": "<", "value": 40}]}
    assert watcher.evaluate_rule(rule, {"pe": 20, "rsi14": 30})[0] is True
    assert watcher.evaluate_rule(rule, {"pe": 20, "rsi14": 50})[0] is False


def test_ops():
    assert watcher.OPS["<"](1, 2) and watcher.OPS[">="](2, 2) and not watcher.OPS[">"](1, 2)


# ---- projection ---------------------------------------------------------

def test_monte_carlo_ordering():
    rng = np.random.default_rng(0)
    prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, 800))
    mc = projection.monte_carlo(_price_df(prices), years=1, amount=100000)
    assert mc["p10_end"] <= mc["median_end"] <= mc["p90_end"]
    assert 0 <= mc["prob_profit"] <= 100


def test_backtest_shape():
    rng = np.random.default_rng(1)
    prices = 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, 900))
    bt = projection.backtest(_price_df(prices), "RSI oversold (<30)")
    assert bt["num_signals"] >= 0 and len(bt["results"]) == 4


# ---- fundamentals / sectors / suggestions / repo_state ------------------

def test_cagr():
    assert fundamentals._cagr(pd.Series([200, 150, 100])) == round((2 ** 0.5 - 1) * 100, 1)
    assert fundamentals._cagr(pd.Series([100])) is None


def test_sector_lookup():
    assert sectors.group_of("TCS") == "IT Services"
    assert sectors.is_financial("HDFCBANK") and not sectors.is_financial("TCS")


def test_analyst_view():
    assert suggestions.analyst_view({"targetMeanPrice": 120}, 100)["upside_pct"] == 20.0
    assert suggestions.analyst_view({}, 100) is None


def test_rule_key_stable_and_distinct():
    base = {"symbol": "TCS", "exchange": "NSE", "label": "a",
            "conditions": [{"metric": "price", "op": ">", "value": 1}]}
    same = dict(base)
    diff = {**base, "conditions": [{"metric": "price", "op": ">", "value": 2}]}
    assert repo_state.rule_key(base) == repo_state.rule_key(same)
    assert repo_state.rule_key(base) != repo_state.rule_key(diff)
