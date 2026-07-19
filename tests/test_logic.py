"""Offline unit tests for the pure logic — no network. Run: pytest -q

These lock in the maths (metrics, scoring, rule evaluation, projection) so future
tweaks can't silently change the numbers the app reports.
"""
import numpy as np
import pandas as pd

from src import (analysis, fundamentals, portfolio, projection, repo_state,
                 sectors, suggestions, verdict, watcher)


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


def test_verdict_quality_and_price():
    # OK-rated + expensive vs peers -> "priced richly" stance, always with points + caveat
    score = {"rating": "OK", "score": 80, "deep": {"q_earnings_yoy": 5}}
    metrics = {"price_vs_ma200": 4, "rsi14": 55}
    peer = {"verdict": {"pe": "pricier than peers"}}
    v = verdict.build(score, metrics, None, peer)
    assert "rich" in v["stance"].lower()
    assert v["points"] and v["watch"] and v["caveat"]

    # Weak + expensive -> high-risk phrasing
    v2 = verdict.build({"rating": "Weak", "score": 20, "deep": {}}, {"price_vs_ma200": -5},
                       {"percentile": 85, "current_pe": 40, "median_pe": 20, "min_pe": 10, "max_pe": 45}, None)
    assert "high-risk" in v2["stance"].lower() or "weak" in v2["stance"].lower()


def test_portfolio_pnl():
    h = {"symbol": "TCS", "qty": 10, "buy_price": 2000.0}
    lot = portfolio.lot_row(h, {"price": 2200.0, "pct_change_day": 1.0})
    assert lot["invested"] == 20000 and lot["value"] == 22000
    assert lot["pnl"] == 2000 and round(lot["pnl_pct"], 1) == 10.0

    # missing price -> lot excluded from totals, counted as missing
    lot2 = portfolio.lot_row({"symbol": "X", "qty": 5, "buy_price": 100.0}, {"price": None})
    tot = portfolio.totals([lot, lot2])
    assert tot["invested"] == 20500 and tot["value"] == 22000
    assert tot["pnl"] == 2000 and tot["missing"] == 1
    # today's move: 22000 value at +1% day -> ~217.8 rupees
    assert abs(tot["day_move"] - (22000 - 22000 / 1.01)) < 0.01


def test_scan_history_roundtrip(tmp_path, monkeypatch):
    from src import scan_history
    monkeypatch.setattr(scan_history, "HISTORY_JSON", tmp_path / "hist.json")
    monkeypatch.setattr(scan_history, "STATE_DIR", tmp_path)
    ranked = [{"symbol": "TCS", "name": "Tata Consultancy", "score": 71,
               "health": {"rating": "OK", "score": 80}, "price": 2269.0,
               "analyst": {"target": 2500.0}}]
    scan_history.append("18 Jul 2026, 20:00", {"universe": "test"}, ranked, {"TCS": "Solid."})
    scans = scan_history.load()
    assert len(scans) == 1 and scans[0]["picks"][0]["price_then"] == 2269.0
    assert scans[0]["picks"][0]["stance"] == "Solid."
    # cap: 25 more appends keep only MAX_SCANS
    for i in range(25):
        scan_history.append(f"t{i}", {}, ranked, {})
    assert len(scan_history.load()) == scan_history.MAX_SCANS


def test_importer_table_angelone_style():
    from src import importer
    # Angel One-ish headers
    df = pd.DataFrame({"Tradingsymbol": ["INFY-EQ", "TCS-EQ", "TOTAL"],
                       "Quantity": [10, 5, None],
                       "Avg. Buy Price": ["1,450.50", 3120, None]})
    rows, err = importer.parse_table(df)
    assert err is None and len(rows) == 2
    assert rows[0] == {"symbol": "INFY", "qty": 10.0, "buy_price": 1450.5}

    # unknown headers -> helpful error
    bad, err2 = importer.parse_table(pd.DataFrame({"Foo": [1], "Bar": [2]}))
    assert bad == [] and "Couldn't find" in err2


def test_importer_paste():
    from src import importer
    rows = importer.parse_text("INFY 10 1450.50\nTCS-EQ 5 3120\nM&M 12 2890.1\ngarbage line")
    syms = [r["symbol"] for r in rows]
    assert syms == ["INFY", "TCS", "M&M"]
    assert rows[0]["qty"] == 10 and rows[0]["buy_price"] == 1450.5
    # price-first order gets swapped by the whole-number heuristic
    r2 = importer.parse_text("CDSL 1150.25 20")
    assert r2[0]["qty"] == 20 and r2[0]["buy_price"] == 1150.25


def test_clean_symbol():
    from src import importer
    assert importer.clean_symbol("NSE: INFY-EQ") == "INFY"
    assert importer.clean_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO"   # real hyphen name kept


def test_rule_key_stable_and_distinct():
    base = {"symbol": "TCS", "exchange": "NSE", "label": "a",
            "conditions": [{"metric": "price", "op": ">", "value": 1}]}
    same = dict(base)
    diff = {**base, "conditions": [{"metric": "price", "op": ">", "value": 2}]}
    assert repo_state.rule_key(base) == repo_state.rule_key(same)
    assert repo_state.rule_key(base) != repo_state.rule_key(diff)
