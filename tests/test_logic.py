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
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "k")   # history is encrypted at rest now
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


def test_importer_header_sniff():
    from src import importer
    # title junk above the real header row (typical broker sheet)
    df = pd.DataFrame([["Your Holding Details", None, None],
                       ["As on 19-07-2026", None, None],
                       ["Symbol", "Qty", "Avg Price"],
                       ["INFY", 10, 1450.5],
                       ["TCS", 5, 3120]])
    rows, err = importer.parse_table(df)
    assert err is None and [r["symbol"] for r in rows] == ["INFY", "TCS"]


def test_clean_symbol():
    from src import importer
    assert importer.clean_symbol("NSE: INFY-EQ") == "INFY"
    assert importer.clean_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO"   # real hyphen name kept


def test_holdings_encryption_roundtrip(tmp_path, monkeypatch):
    from src import repo_state
    monkeypatch.setattr(repo_state, "HOLDINGS_JSON", tmp_path / "holdings.json")
    monkeypatch.setattr(repo_state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "test-key-123")
    data = [{"symbol": "TCS", "exchange": "NSE", "qty": 10, "buy_price": 2000.0, "buy_date": None}]
    repo_state._write_holdings(data)

    on_disk = (tmp_path / "holdings.json").read_text()
    assert "TCS" not in on_disk and '"encrypted": true' in on_disk   # ciphertext only
    assert repo_state._read_holdings_raw() == data                    # decrypts back

    # unchanged data -> file NOT rewritten (no ciphertext churn / commits)
    before = on_disk
    repo_state._write_holdings(data)
    assert (tmp_path / "holdings.json").read_text() == before

    # wrong/no key -> unreadable, degrades to None (Action-safe)
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "another-key")
    assert repo_state._read_holdings_raw() is None
    monkeypatch.delenv("STOCKWATCH_STATE_KEY")
    assert repo_state._read_holdings_raw() is None


def test_write_private_migrates_and_refuses(tmp_path, monkeypatch):
    import json
    from src import repo_state
    monkeypatch.setattr(repo_state, "STATE_DIR", tmp_path)
    p = tmp_path / "rules.json"
    data = [{"symbol": "PVRINOX", "conditions": [{"metric": "price", "value": 1240}]}]

    # no key -> refuse, never leave private plaintext behind
    monkeypatch.delenv("STOCKWATCH_STATE_KEY", raising=False)
    assert repo_state._write_private(p, data) is False and not p.exists()

    # a legacy PLAINTEXT file with identical content must still get migrated
    p.write_text(json.dumps(data))
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "k1")
    assert repo_state._write_private(p, data) is True
    txt = p.read_text()
    assert "PVRINOX" not in txt and "1240" not in txt and '"encrypted": true' in txt
    assert repo_state._read_maybe_enc(p, None) == data

    # already-encrypted + unchanged -> not rewritten (no churn)
    before = p.read_text()
    assert repo_state._write_private(p, data) is True
    assert p.read_text() == before


def test_finance_plan_roundtrip(tmp_path, monkeypatch):
    from src import finance_plan
    monkeypatch.setattr(finance_plan, "PLAN_JSON", tmp_path / "finance_plan.json")
    monkeypatch.setattr(finance_plan, "STATE_DIR", tmp_path)

    # no key -> refuses to write (a plaintext plan must never reach the repo)
    monkeypatch.delenv("STOCKWATCH_STATE_KEY", raising=False)
    assert finance_plan.save_plan("# secret") is False
    assert not (tmp_path / "finance_plan.json").exists()

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "test-key-123")
    assert finance_plan.save_plan("# My plan\nSIP 78k") is True
    on_disk = (tmp_path / "finance_plan.json").read_text()
    assert "My plan" not in on_disk and '"encrypted": true' in on_disk
    assert finance_plan.load_plan()["content"] == "# My plan\nSIP 78k"

    # unchanged content -> file not rewritten (no ciphertext churn)
    before = on_disk
    assert finance_plan.save_plan("# My plan\nSIP 78k") is True
    assert (tmp_path / "finance_plan.json").read_text() == before

    # wrong key -> unreadable, degrades to None
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "other-key")
    assert finance_plan.load_plan() is None


def test_reminders_yearly_and_due(tmp_path, monkeypatch):
    from datetime import date
    from src import reminders
    monkeypatch.setattr(reminders, "REMINDERS_JSON", tmp_path / "reminders.json")
    monkeypatch.setattr(reminders, "STATE_DIR", tmp_path)

    ppf = reminders.new("PPF 1.5L", "2027-04-01", yearly=True)
    once = reminders.new("FD matures", "2026-08-05")

    # yearly rolls to the upcoming Apr 1 regardless of stored year
    assert reminders.effective_date(ppf, date(2026, 8, 1)) == date(2027, 4, 1)
    assert reminders.effective_date(ppf, date(2027, 6, 1)) == date(2028, 4, 1)
    # due only inside the 7-day window; one-off overdue still counts
    assert reminders.due(ppf, date(2027, 3, 28)) is True
    assert reminders.due(ppf, date(2027, 1, 1)) is False
    assert reminders.due(once, date(2026, 8, 1)) is True          # 4 days out
    once["done"] = True
    assert reminders.due(once, date(2026, 8, 1)) is False         # done one-off drops

    # no key -> refuse to write private data
    monkeypatch.delenv("STOCKWATCH_STATE_KEY", raising=False)
    assert reminders.save([ppf]) is False and not (tmp_path / "reminders.json").exists()
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "k1")
    assert reminders.save([ppf]) is True
    assert "PPF" not in (tmp_path / "reminders.json").read_text()  # ciphertext only
    assert reminders.load() == [ppf]


def test_reminders_monthly_until():
    from datetime import date
    from src import reminders

    stp = reminders.new("Redeem 80k arbitrage", "2026-09-18",
                        monthly=True, until="2027-06-30")
    # before the start date it points at the start date itself
    assert reminders.effective_date(stp, date(2026, 8, 10)) == date(2026, 9, 18)
    # after a run passes, it rolls to the 18th of the next month
    assert reminders.effective_date(stp, date(2026, 9, 19)) == date(2026, 10, 18)
    # December rolls over the year boundary
    assert reminders.effective_date(stp, date(2026, 12, 20)) == date(2027, 1, 18)
    # past `until` it stops firing entirely
    assert reminders.effective_date(stp, date(2027, 6, 19)) is None
    assert reminders.due(stp, date(2027, 7, 1)) is False
    # due inside the 7-day window, quiet outside it
    assert reminders.due(stp, date(2026, 9, 12)) is True
    assert reminders.due(stp, date(2026, 9, 1)) is False
    # done never silences a repeating reminder
    stp["done"] = True
    assert reminders.due(stp, date(2026, 9, 18)) is True

    # a monthly anchored on the 31st clamps to shorter months
    eom = reminders.new("month-end sweep", "2026-08-31", monthly=True)
    assert reminders.effective_date(eom, date(2026, 9, 1)) == date(2026, 9, 30)


def test_plan_checklist_toggle():
    from src import finance_plan
    content = ("## Pending\n- [ ] open PPF\n- [x] SIPs live\nsome prose\n"
               "  - [ ] nested task\n")
    items = finance_plan.checklist_items(content)
    assert [i["done"] for i in items] == [False, True, False]
    assert items[0]["text"] == "open PPF" and items[0]["line"] == 1

    done = finance_plan.set_check(content, 1, True)
    assert "- [x] open PPF" in done and done.endswith("\n")   # trailing newline kept
    assert finance_plan.checklist_items(done)[0]["done"] is True
    # untick, and non-checkbox line is a no-op
    assert "- [ ] open PPF" in finance_plan.set_check(done, 1, False)
    assert finance_plan.set_check(content, 3, True) == content   # 'some prose' unchanged


def test_mf_store_and_valuation(tmp_path, monkeypatch):
    from src import mf
    monkeypatch.setattr(mf, "MF_JSON", tmp_path / "mf_holdings.json")
    monkeypatch.setattr(mf, "STATE_DIR", tmp_path)

    rows = [{"name": "Parag Parikh Flexi Cap Direct", "code": "122639",
             "units": 100.0, "invested": 8000.0, "est_value": None, "note": None},
            {"name": "ICICI Arbitrage Direct", "code": "120364",
             "units": None, "invested": 800000.0, "est_value": 800000.0,
             "note": "allotting"}]

    # no key -> refuses to write (real positions must never reach the repo plain)
    monkeypatch.delenv("STOCKWATCH_STATE_KEY", raising=False)
    assert mf.save_mf(rows) is False
    assert not (tmp_path / "mf_holdings.json").exists()

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "test-key-123")
    assert mf.save_mf(rows) is True
    on_disk = (tmp_path / "mf_holdings.json").read_text()
    assert "Parag" not in on_disk and '"encrypted": true' in on_disk
    assert mf.load_mf() == rows

    # unchanged rows -> file not rewritten (no ciphertext churn)
    before = on_disk
    assert mf.save_mf(list(rows)) is True
    assert (tmp_path / "mf_holdings.json").read_text() == before

    # valuation: units + NAV -> live; no units -> estimate; P&L needs invested
    live = mf.value_row(rows[0], {"nav": 90.0, "date": "28-07-2026"})
    assert live["value"] == 9000.0 and live["pnl"] == 1000.0
    assert live["source"] == "live 28-07-2026" and round(live["pnl_pct"], 1) == 12.5
    est = mf.value_row(rows[1], None)
    assert est["value"] == 800000.0 and est["pnl"] == 0.0 and est["source"] == "estimate"

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "other-key")
    assert mf.load_mf() is None


def test_advice_ledger_roundtrip(tmp_path, monkeypatch):
    from src import advice
    monkeypatch.setattr(advice, "ADVICE_JSON", tmp_path / "advice.json")
    monkeypatch.setattr(advice, "STATE_DIR", tmp_path)

    rows = [advice.new_entry("pvrinox", "HOLD-RULE", "bounded catalyst-wait",
                             catalyst="H2 box office", catalyst_date="2026-11-15",
                             sell_above=1240, stop_below=1000, review_by="2026-12-31")]
    assert rows[0]["symbol"] == "PVRINOX" and rows[0]["status"] == "OPEN"

    monkeypatch.delenv("STOCKWATCH_STATE_KEY", raising=False)
    assert advice.save_advice(rows) is False           # no key -> never written plain
    assert not (tmp_path / "advice.json").exists()

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "test-key-123")
    assert advice.save_advice(rows) is True
    on_disk = (tmp_path / "advice.json").read_text()
    assert "PVRINOX" not in on_disk and '"encrypted": true' in on_disk
    assert advice.load_advice() == rows

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "other-key")
    assert advice.load_advice() is None


def test_advice_alert_rules_and_due():
    from datetime import date
    from src import advice
    rows = [
        advice.new_entry("PVR", "HOLD-RULE", "x", sell_above=1240, stop_below=1000,
                         catalyst_date="2026-11-15"),
        advice.new_entry("AAA", "KEEP", "y", stop_below=100, review_by="2030-01-01"),
        advice.new_entry("BBB", "SELL", "z"),          # no bands -> no rules
    ]
    rows[2]["status"] = "DONE-RIGHT"                    # closed -> excluded everywhere
    ruleset = advice.alert_rules_from(rows)
    labels = [r["label"] for r in ruleset]
    assert len(ruleset) == 3                            # PVR hi+lo, AAA lo
    assert all(advice.is_advice_rule(l) for l in labels)
    assert all(r["mode"] == "edge" for r in ruleset)

    today = date(2026, 11, 10)
    assert advice.due_soon(rows[0], today) is True      # catalyst 5 days out
    assert advice.due_soon(rows[1], today) is False     # review years away
    assert advice.due_soon(rows[2], today) is False     # closed

    assert advice.pretty_date("2026-08-03") == "3rd Aug, 26"
    assert advice.pretty_date("2026-11-15") == "15th Nov, 26"
    assert advice.pretty_date("Monday session") == "Monday session"   # non-ISO passes through
    assert advice.pretty_date("") == "" and advice.pretty_date(None) == ""


def test_negative_cache(monkeypatch):
    from src import datasource as ds
    ds._CACHE.clear()
    ds._remember_miss("k1")
    assert ds._cached("k1") is ds._MISS            # fresh failure -> MISS sentinel
    # expired failure -> None (retry allowed)
    ds._CACHE["k1"] = (0, ds._MISS)
    assert ds._cached("k1") is None
    # real values still work
    ds._store("k2", {"a": 1})
    assert ds._cached("k2") == {"a": 1}
    ds._CACHE.clear()


def test_rule_key_stable_and_distinct():
    base = {"symbol": "TCS", "exchange": "NSE", "label": "a",
            "conditions": [{"metric": "price", "op": ">", "value": 1}]}
    same = dict(base)
    diff = {**base, "conditions": [{"metric": "price", "op": ">", "value": 2}]}
    assert repo_state.rule_key(base) == repo_state.rule_key(same)
    assert repo_state.rule_key(base) != repo_state.rule_key(diff)


def test_advisor_action_parsing():
    """The advisor's proposals must survive a round trip, and anything malformed
    must be dropped rather than reaching a confirm button."""
    from src import advisor_bot as ab

    reply = ('You should look at this again in October.\n\n'
             '```actions\n'
             '[{"type": "add_reminder", "text": "Review ITC", "date": "2026-10-01",'
             ' "yearly": false, "why": "the October check-in"}]\n'
             '```')
    prose, actions = ab.split_actions(reply)
    assert "```" not in prose and prose.startswith("You should look")
    assert len(actions) == 1 and actions[0]["type"] == "add_reminder"
    assert "Review ITC" in ab.describe_action(actions[0])
    assert "1st Oct, 26" in ab.describe_action(actions[0])

    # plain answer, no block
    assert ab.split_actions("just prose") == ("just prose", [])
    # unknown type and broken JSON are both dropped
    assert ab.split_actions('x\n```actions\n[{"type":"rm -rf"}]\n```')[1] == []
    assert ab.split_actions("x\n```actions\n[not json\n```")[1] == []


def test_advisor_apply_action_validates(tmp_path, monkeypatch):
    """apply_action re-checks every field itself — the model is never trusted."""
    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "k")
    monkeypatch.setattr(repo_state, "STATE_DIR", tmp_path)
    from src import advisor_bot as ab, reminders as rem
    monkeypatch.setattr(rem, "REMINDERS_JSON", tmp_path / "reminders.json")

    ok, msg = ab.apply_action({"type": "add_reminder", "text": "PPF top-up",
                               "date": "2027-04-01", "yearly": True})
    assert ok, msg
    rows = rem.load()
    assert len(rows) == 1 and rows[0]["yearly"] is True

    # bad date / missing text / unknown type all refuse, and write nothing
    assert ab.apply_action({"type": "add_reminder", "text": "x", "date": "soon"})[0] is False
    assert ab.apply_action({"type": "add_reminder", "text": "", "date": "2027-04-01"})[0] is False
    assert ab.apply_action({"type": "drop_table"})[0] is False
    assert ab.apply_action({"type": "add_alert", "symbol": "TCS", "metric": "vibes",
                            "op": "<", "value": 1})[0] is False
    assert ab.apply_action({"type": "add_alert", "symbol": "TCS", "metric": "price",
                            "op": "<", "value": "abc"})[0] is False
    assert len(rem.load()) == 1                      # still just the one good write


def test_ipo_verdicts():
    from src import ipo

    # mainboard passing every bar
    v, why = ipo.verdict({"sme": False, "gmp_pct": 28.0, "total": 40.0, "qib": 12.0})
    assert v == "APPLY-ZONE" and "1 lot" in why
    # good GMP but book still filling -> watch, not skip
    v, _ = ipo.verdict({"sme": False, "gmp_pct": 30.0, "total": 0.4, "qib": 0.1})
    assert v == "WATCH"
    # retail froth without QIBs fails the honesty check
    v, why = ipo.verdict({"sme": False, "gmp_pct": 25.0, "total": 30.0, "qib": 1.0})
    assert v == "WATCH" and "QIB" in why
    # SME bars are stricter: 25% GMP passes mainboard but not SME
    assert ipo.verdict({"sme": True, "gmp_pct": 25.0, "total": 60.0, "qib": 4.0})[0] == "SKIP"
    assert ipo.verdict({"sme": True, "gmp_pct": 39.0, "total": 55.0, "qib": 3.6})[0] == "APPLY-ZONE"
    # nothing known
    assert ipo.verdict({"sme": False, "gmp_pct": None, "total": None})[0] == "NO DATA"

    # helpers used to join the two scraped tables
    assert ipo._norm("LAPL Automotive IPO (SME)") == ipo._norm("LAPL Automotive Ltd")
    assert ipo._num("₹2,25,600") == 225600.0
    assert ipo._num("55.16x") == 55.16
    assert ipo._num("--") is None


def test_shop_judge_and_parse():
    from src import shop

    # mass-proven listing clears the pick bar
    v, why = shop.judge({"rating": 4.1, "reviews": 8885, "price": 909, "mrp": 1099})
    assert v == "PICK-ZONE" and "8,885" in why
    # fake-MRP anchor gets called out but doesn't sink a good product
    v, why = shop.judge({"rating": 4.0, "reviews": 112087, "price": 569, "mrp": 3999})
    assert v == "PICK-ZONE" and "inflated" in why
    # below the rating floor no price is cheap enough
    assert shop.judge({"rating": 3.5, "reviews": 864, "price": 400, "mrp": None})[0] == "AVOID"
    # great stars on a thin review base stay risky
    assert shop.judge({"rating": 4.3, "reviews": 66, "price": 999, "mrp": None})[0] == "RISKY"
    # middle of the road
    assert shop.judge({"rating": 3.8, "reviews": 7564, "price": 948, "mrp": None})[0] == "OK"
    assert shop.judge({"rating": None, "reviews": None, "price": 500, "mrp": None})[0] == "UNRATED"

    # flipkart parser works off structure, not their obfuscated classes
    fk = """<div data-id="X1"><a href="/asian-shoe/p/itm123?pid=9">
            <img alt=""/></a><a href="/asian-shoe/p/itm123">ASIAN WNDR-13 Pro</a>
            <div>4.2 (1,401) ₹839 ₹1,499 44% off</div></div>
            <div data-id="X2"><a href="/no-price/p/itm999"><img alt=""/></a></div>"""
    rows = shop._parse_flipkart(fk)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "ASIAN WNDR-13 Pro" and r["price"] == 839
    assert r["mrp"] == 1499 and r["rating"] == 4.2 and r["reviews"] == 1401
    assert r["url"].endswith("/asian-shoe/p/itm123")
    # price cap filters
    assert shop._parse_flipkart(fk, max_price=800) == []
    assert shop._parse_flipkart("") == []

    # amazon parser: title falls back to the image alt when h2 is brand-only
    amz = """<div data-component-type="s-search-result">
             <img alt="Sponsored Ad - SPARX Sports Shoe SM-171 for Men"/>
             <h2><span>SPARX</span></h2>
             <a class="a-link-normal" href="/Sparx-SM-171/dp/B07YP2F21T/ref=xyz"></a>
             <span class="a-price-whole">909</span>
             <span class="a-price a-text-price"><span class="a-offscreen">₹1,099</span></span>
             <span class="a-icon-alt">4.1 out of 5 stars</span>
             <span aria-label="8,885 ratings"></span></div>"""
    rows = shop._parse_amazon(amz)
    assert len(rows) == 1
    r = rows[0]
    assert "SM-171" in r["title"] and r["price"] == 909 and r["mrp"] == 1099
    assert r["rating"] == 4.1 and r["reviews"] == 8885
    assert r["url"] == "https://www.amazon.in/Sparx-SM-171/dp/B07YP2F21T"

    assert shop.search_urls("white shoes")["Amazon"].endswith("k=white+shoes")
