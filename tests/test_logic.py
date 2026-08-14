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
    assert watcher.evaluate_rule(rule, {"price": 40}) == \
        (True, ["the price is ₹40 — under your ₹50 line"], True)
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


def test_ipo_investorgain_parse():
    from src import ipo
    from datetime import date

    # rows shaped exactly like the live report API (display HTML and all)
    data = {"reportTableData": [
        {"Name": '<a href="/subscription/x/1854/" title="Technocraft Ventures">'
                 'Technocraft Ventures</a><br><span class="badge">IPO</span>'
                 '<small><b>GMP:&#8377;<b>24</b> (11.32%)</b></small>',
         "Total": '<b>15.26</b><br><small><b>11th Aug 13:10</b></small>',
         "QIB": "8.86", "NII": "27.61", "RII": "13.63", "IPO Price": "212",
         "~end_dt": "2026-08-11 00:00:00", "Closing Date": "11-08-2026",
         "~IPO_Category": "IPO"},
        {"Name": '<a href="/subscription/y/2/" title="Sham Foam">Sham Foam</a>'
                 '<small><b>GMP:&#8377;<b>--</b> (0.00%)</b></small>',
         "Total": "-", "QIB": "-", "NII": "-", "RII": "-", "IPO Price": "80",
         "~end_dt": "2026-08-13 00:00:00", "~IPO_Category": "SME"},
        {"Name": "<span>no title attr = unreadable, skipped</span>"},
    ]}
    rows = ipo._parse_investorgain(data)
    assert len(rows) == 2
    t = rows[0]
    assert t["name"] == "Technocraft Ventures" and t["sme"] is False
    assert t["gmp"] == 24.0 and t["gmp_pct"] == 11.3
    assert t["total"] == 15.26 and t["qib"] == 8.86 and t["retail"] == 13.63
    assert t["price"] == 212.0
    assert t["updated"] == "11th Aug 13:10"
    assert t["_closes"] == date(2026, 8, 11) and "August 11" in t["close"]
    s = rows[1]
    assert s["sme"] is True
    assert s["gmp"] is None and s["gmp_pct"] is None    # '--' = no GMP quoted
    assert s["total"] is None and s["qib"] is None

    # financial-year path segment (Indian FY, April cutover)
    assert ipo._fy(date(2026, 8, 11)) == "2026-27"
    assert ipo._fy(date(2027, 2, 1)) == "2026-27"
    assert ipo._fy(date(2027, 4, 1)) == "2027-28"


def test_heartbeat_reminder_split(monkeypatch):
    from datetime import timedelta
    from src import clock, heartbeat, reminders

    today = clock.ist_today()
    rows = [
        reminders.new("pay advance tax", today.isoformat()),
        reminders.new("IPO last day: apply", (today + timedelta(days=1)).isoformat()),
        reminders.new("check allotment", (today + timedelta(days=4)).isoformat()),
        reminders.new("renew FD", (today - timedelta(days=2)).isoformat()),
        reminders.new("far away", (today + timedelta(days=30)).isoformat()),
    ]
    monkeypatch.setattr(heartbeat.reminders, "load", lambda: rows)
    b = heartbeat.reminder_buckets(today)
    # a past date is never lumped in with today's jobs, and a future one never
    # appears under a "today" header — that mix is what made the mail unreadable
    assert [i["text"] for i in b["today"]] == ["pay advance tax"]
    assert [i["text"] for i in b["overdue"]] == ["renew FD"]
    assert [i["text"] for i in b["upcoming"]] == ["IPO last day: apply",
                                                  "check allotment"]
    assert "was due" in heartbeat.render_overdue(b["overdue"], today)[0]
    assert "2 days ago" in heartbeat.render_overdue(b["overdue"], today)[0]
    up = heartbeat.render_upcoming(b["upcoming"], today)
    assert "(tomorrow)" in up[0] and "(in 4 days)" in up[1]
    assert clock.short(today + timedelta(days=4)) in up[1]   # the date itself, always


def test_heartbeat_overdue_capped(monkeypatch):
    from datetime import timedelta
    from src import clock, heartbeat, reminders

    today = clock.ist_today()
    rows = [reminders.new(f"old job {n}", (today - timedelta(days=n)).isoformat())
            for n in range(1, 8)]
    monkeypatch.setattr(heartbeat.reminders, "load", lambda: rows)
    lines = heartbeat.render_overdue(heartbeat.reminder_buckets(today)["overdue"], today)
    assert len(lines) == heartbeat._OVERDUE_SHOWN + 1
    assert "and 3 more past their date" in lines[-1]
    assert "yesterday" in lines[0]                  # newest overdue listed first


def test_clock_ist_and_wording():
    from datetime import date, datetime, timedelta, timezone
    from src import clock

    # a UTC evening is already the next day in India — the whole reason this exists
    late = datetime(2026, 8, 14, 19, 30, tzinfo=timezone.utc)
    assert late.astimezone(clock.IST).date() == date(2026, 8, 15)
    assert clock.to_ist("2026-08-14T09:42:00+00:00").hour == 15
    assert clock.clock_time(datetime(2026, 8, 14, 15, 45, tzinfo=clock.IST)) == "3:45 pm"
    assert clock.stamp(datetime(2026, 8, 14, 15, 45, tzinfo=clock.IST)) == \
        "Friday, 14 August 2026 · 3:45 pm IST"

    today = date(2026, 8, 14)
    assert clock.short(today) == "Fri 14 Aug"
    assert clock.when(today, today) == "Fri 14 Aug (today)"
    assert clock.when(today + timedelta(days=1), today) == "Sat 15 Aug (tomorrow)"
    assert clock.when(today + timedelta(days=3), today) == "Mon 17 Aug (in 3 days)"
    assert clock.when(today - timedelta(days=1), today) == "Thu 13 Aug (yesterday)"
    assert clock.when(today - timedelta(days=2), today) == "Wed 12 Aug (2 days ago)"


def test_money_formatting():
    from src import fmt

    assert fmt.inr(412340) == "₹4,12,340"          # Indian grouping, not 412,340
    assert fmt.inr(1234567) == "₹12,34,567"
    assert fmt.inr(-1890) == "-₹1,890"
    assert fmt.inr(999) == "₹999" and fmt.inr(None) == "—"
    assert fmt.signed_inr(52400) == "+₹52,400"
    assert fmt.pct(-0.48) == "-0.5%" and fmt.pct(14.62) == "+14.6%"
    assert fmt.money_dot(500) == "🟢" and fmt.money_dot(-500) == "🔴"
    assert fmt.move(-0.5) == "▼ -0.5%" and fmt.move(0.7) == "▲ +0.7%"


def test_positions_rolled_up_per_symbol():
    from src import portfolio

    lots = [
        portfolio.lot_row({"symbol": "SUZLON", "qty": 100, "buy_price": 50},
                          {"price": 60, "pct_change_day": 1.0}),
        portfolio.lot_row({"symbol": "SUZLON", "qty": 100, "buy_price": 70},
                          {"price": 60, "pct_change_day": 1.0}),
        portfolio.lot_row({"symbol": "ITC", "qty": 10, "buy_price": 400},
                          {"price": None, "pct_change_day": None}),
    ]
    pos = portfolio.by_symbol(lots)
    suzlon = next(p for p in pos if p["symbol"] == "SUZLON")
    assert suzlon["qty"] == 200 and suzlon["buy_price"] == 60.0   # weighted average
    assert suzlon["value"] == 12000 and suzlon["pnl"] == 0
    itc = next(p for p in pos if p["symbol"] == "ITC")
    assert itc["value"] is None and itc["pnl"] is None   # unpriced, not zero
    assert pos[0]["symbol"] == "SUZLON"                  # biggest first


def test_portfolio_block_wording():
    from src import heartbeat

    out = heartbeat.portfolio_block({"invested": 149244, "value": 132168,
                                     "day_move": -1544.0, "day_pct": -1.16,
                                     "pnl": -8394.0, "pnl_pct": -5.62,
                                     "missing": 2})
    text = "\n".join(out)
    assert "Worth ₹1,32,168 now" in text and "you put in ₹1,49,244" in text
    assert "Today: 🔴 -₹1,544 (-1.2%)" in text
    assert "Since you bought: 🔴 -₹8,394 (-5.6%)" in text
    assert "2 holdings had no price" in text
    assert heartbeat.portfolio_block({}) == []          # nothing owned, no block


def test_insights_are_derived_from_real_numbers():
    from datetime import timedelta
    from src import clock, insights

    today = clock.ist_today()
    positions = [
        {"symbol": "ASIANPAINT", "value": 51000.0, "pnl": 2852.0, "pnl_pct": 5.9},
        {"symbol": "ITC", "value": 5560.0, "pnl": -2867.0, "pnl_pct": -34.0},
        {"symbol": "SUZLON", "value": 3000.0, "pnl": -1514.0, "pnl_pct": -33.4},
        {"symbol": "GRAPHITE", "value": 5054.0, "pnl": 1059.0, "pnl_pct": 26.5},
        {"symbol": "NIPPON ETF JUNI.", "value": None, "pnl": None, "pnl_pct": None},
    ]
    totals = {"value": 64614.0, "pnl": -3470.0}

    # concentration: 51000/64614 = 79%, and the tip must say so
    conc = insights.concentration(positions, totals["value"])
    assert conc and "79%" in conc[0]["text"] and "ASIANPAINT" in conc[0]["text"]
    assert insights.concentration(positions[1:], 20000.0) == []   # nothing dominant

    # a big loss only earns a tip when the fundamentals are weak too
    assert insights.broken_thesis(positions, {"ITC": "OK"}) == []
    broken = insights.broken_thesis(positions, {"ITC": "Weak"})
    assert broken and "ITC is down 34%" in broken[0]["text"]

    # unprotected: ITC and SUZLON are down >10% with no stop anywhere
    bare = insights.unprotected_losers(positions, [], [])
    assert {t["key"] for t in bare} == {"nostop:ITC", "nostop:SUZLON"}
    # a stop in the ledger, or a price-below alert rule, both count as covered
    guarded = insights.unprotected_losers(
        positions, [{"symbol": "ITC", "status": "OPEN", "stop_below": 250}],
        [{"symbol": "SUZLON", "active": 1,
          "conditions": [{"metric": "price", "op": "<", "value": 40}]}])
    assert guarded == []

    won = insights.unplanned_winners(positions, [])
    assert won and "GRAPHITE is up 26%" in won[0]["text"]
    assert insights.unplanned_winners(
        positions, [{"symbol": "GRAPHITE", "status": "OPEN",
                     "sell_above": 900}]) == []

    gaps = insights.data_gaps(positions, [{"name": "x", "units": None}])
    assert any("NIPPON ETF JUNI." in t["text"] for t in gaps)
    assert any(t["key"] == "mfgaps" for t in gaps)

    # tax: only when the 12-month line is close AND the position is in profit
    holdings = [{"symbol": "GRAPHITE", "qty": 7, "buy_price": 570.0,
                 "buy_date": (today - timedelta(days=340)).isoformat()},
                {"symbol": "ITC", "qty": 20, "buy_price": 420.0,
                 "buy_date": (today - timedelta(days=340)).isoformat()}]
    lt = insights.ltcg_countdown(holdings, {"GRAPHITE": 722.0, "ITC": 278.0}, today)
    assert len(lt) == 1 and "GRAPHITE turns long-term in 25 days" in lt[0]["text"]
    assert "12.5%" in lt[0]["why"] and "20%" in lt[0]["why"]
    # far from the line, or a loss, earns nothing
    assert insights.ltcg_countdown(
        [{**holdings[0], "buy_date": (today - timedelta(days=30)).isoformat()}],
        {"GRAPHITE": 722.0}, today) == []

    assert insights.missing_buy_dates([{"symbol": "X"}, holdings[0]])[0]["text"] \
        .startswith("1 of your 2 holdings")
    assert insights.missing_buy_dates([{"symbol": "X"}])[0]["text"] \
        .startswith("None of your 1 holdings")

    # the share must be of the losses, not of the net figure: winners offsetting
    # them once produced "105% of your total loss"
    conc_loss = insights.loss_is_concentrated(positions, totals)
    assert conc_loss and int(conc_loss[0]["text"].split("%")[0]) <= 100
    assert "everything you're down" in conc_loss[0]["text"]

    # a standing rule true for days is noise, not information
    # a loss too big to be a loss: flag it as a cost-basis artefact, not a crash
    odd = insights.suspect_cost_basis(
        positions + [{"symbol": "ITCHOTELS", "pnl_pct": -75.0, "pnl": -330.0,
                      "value": 330.0}])
    assert odd and "ITCHOTELS -75.0%" in odd[0]["text"]
    assert "demerger" in odd[0]["why"]
    assert insights.suspect_cost_basis(positions) == []   # -34% is just a loss

    nag = insights.nagging_rule([{"id": 1, "symbol": "INFY", "active": 1,
                                  "mode": "level", "label": "below 200dma dip",
                                  "true_since": (today - timedelta(days=9)).isoformat()}],
                                today)
    assert nag and "true for 9 days straight" in nag[0]["text"]


def test_insight_choice_is_stable_and_respects_settings():
    from src import insights

    tips = [insights.tip("urgent", "risk", 80, "urgent thing"),
            insights.tip("mid", "tax", 50, "middling thing"),
            insights.tip("low", "ipo", 12, "house note")]
    # urgent always shows, and the same seed picks the same companions
    first = insights.choose(tips, n=2, seed=7)
    assert first[0]["key"] == "urgent"
    assert [t["key"] for t in first] == [t["key"] for t in insights.choose(tips, n=2, seed=7)]
    # switching a category off removes it entirely
    assert all(t["category"] != "ipo" for t in
               insights.choose(tips, n=3, seed=1, categories=["risk", "tax"]))
    # urgent-only mode drops the general ones
    only = insights.choose(tips, n=3, seed=1, min_urgency=60)
    assert [t["key"] for t in only] == ["urgent"]
    assert insights.choose([], n=2) == []


def test_settings_roundtrip_and_defaults(tmp_path, monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "STATE_DIR", tmp_path)
    monkeypatch.setattr(settings, "SETTINGS_JSON", tmp_path / "settings.json")
    assert settings.load() == settings.DEFAULTS          # nothing saved yet
    assert settings.get("banner") is True

    settings.save({"banner": False, "banner_tips": 3})
    got = settings.load()
    assert got["banner"] is False and got["banner_tips"] == 3
    assert got["explainers"] is True                     # untouched key kept
    assert set(got) == set(settings.DEFAULTS)            # no junk keys

    (tmp_path / "settings.json").write_text("{not json")
    assert settings.load() == settings.DEFAULTS          # corrupt file can't break it


def test_html_mail_colours_each_number_for_itself():
    from src import fmt, mailhtml

    # ICICIBANK: UP today, but your position is in loss. The day's move must be
    # green and the position red — one leading red dot for both read as "the
    # stock is down today", which it isn't.
    rows = mailhtml.stock_rows(
        [{"symbol": "ICICIBANK", "price": 1417.0, "day_pct": 0.7, "qty": 3,
          "buy_price": 1439.67, "value": 4251.0, "pnl": -68.0, "pnl_pct": -1.6}],
        [], None, fmt)
    def colour_of(needle: str) -> str:
        """The colour declared on the span that prints this number."""
        head = rows[:rows.index(needle)]
        return head[head.rindex("color:"):][6:13]
    assert colour_of("+0.7%") == mailhtml.GREEN     # today was up
    assert colour_of("-1.6%") == mailhtml.RED       # the position is in loss
    assert "▲" in rows and "down ₹68" in rows
    # LTP, your average cost and what it's worth — and no intraday high-low
    # range, which told you nothing you'd act on
    assert "LTP ₹1,417" in rows and "avg ₹1,440" in rows
    assert "worth ₹4,251" in rows and "day ₹" not in rows

    # a no-price holding says so instead of showing a zero
    blank = mailhtml.stock_rows([{"symbol": "ITC", "price": None, "day_pct": None,
                                  "qty": 20, "value": None, "pnl": None,
                                  "pnl_pct": None}], [], None, fmt)
    assert "no price" in blank and "you hold 20" in blank

    card = mailhtml.money_card({"invested": 149244, "value": 132168,
                                "day_move": -1544.0, "day_pct": -1.16,
                                "pnl": -8394.0, "pnl_pct": -5.62, "missing": 0}, fmt)
    assert "₹1,32,168" in card and "-₹1,544" in card and mailhtml.RED in card
    assert mailhtml.money_card({}, fmt) == ""

    # anything interpolated is escaped — a company name with an ampersand or a
    # stray bracket must not become markup
    danger = mailhtml.ipo_rows([{"name": 'Q&T <b>Foods', "kind": "SME",
                                 "verdict": "SKIP", "numbers": "premium 0.9%",
                                 "closes": "closes today", "last_day": True}])
    assert "Q&amp;T &lt;b&gt;Foods" in danger and "<b>Foods" not in danger
    full = mailhtml.page("T&C", "sub <x>", [card], "foot & note")
    assert "T&amp;C" in full and "sub &lt;x&gt;" in full


def test_ipo_brief_groups_by_action(monkeypatch):
    from datetime import timedelta
    from src import clock, ipo

    today = clock.ist_today()
    rows = [
        {"name": "Shiprocket", "sme": False, "verdict": "APPLY-ZONE",
         "why": "all bars ok", "gmp": 35.0, "gmp_pct": 36.1, "price": 97.0,
         "total": 102.28, "qib": 125.2, "closes": today, "close": "x",
         "updated": "14th Aug 13:10", "source": "investorgain.com (live)"},
        {"name": "Dhoot Transmission", "sme": False, "verdict": "WATCH",
         "why": "GMP qualifies; recheck subscription on the last day",
         "gmp": 246.0, "gmp_pct": 28.2, "price": 872.0, "total": 3.06,
         "qib": 4.59, "closes": today, "close": "x", "updated": "",
         "source": "investorgain.com (live)"},
        {"name": "Later Co", "sme": False, "verdict": "APPLY-ZONE", "why": "ok",
         "gmp_pct": 25.0, "total": 20.0, "qib": 6.0, "close": "x",
         "closes": today + timedelta(days=3), "updated": "",
         "source": "investorgain.com (live)"},
        {"name": "Q&T Foods", "sme": True, "verdict": "SKIP", "why": "no",
         "gmp_pct": 0.9, "total": 1.21, "qib": None, "closes": today,
         "close": "x", "updated": "", "source": "investorgain.com (live)"},
        {"name": "Skytech", "sme": True, "verdict": "SKIP", "why": "no",
         "gmp_pct": 16.9, "total": 0.26, "qib": 0.0, "closes": today,
         "close": "x", "updated": "", "source": "investorgain.com (live)"},
    ]
    monkeypatch.setattr(ipo, "screen", lambda: rows)
    b = ipo.brief(today)

    # only same-day deadlines become to-dos: the one closing in 3 days must not
    assert len(b["todo"]) == 2
    assert "Apply for Shiprocket" in b["todo"][0]
    assert "today is the last day, bids close at 4 pm" in b["todo"][0]
    assert "Decide on Dhoot Transmission" in b["todo"][1]
    assert "crosses 15x with QIB over 5x" in b["todo"][1]
    assert not any("Later Co" in t for t in b["todo"])
    assert any("closes Mon" in a or "closes " in a for a in b["act"])
    # the five-rows-of-SKIP table collapses to one line
    assert b["skip"] == "Not worth it today (2): Q&T Foods (0.9%), Skytech (16.9%)"
    assert "as of 14th Aug 13:10" in b["footer"]
    assert "grey-market premium 36.1% (₹35 over the ₹97 price)" in b["act"][0]

    assert ipo.closing_phrase({"closes": today}, today) == \
        "closes today — last day to apply"
    assert ipo.closing_phrase({"closes": today + timedelta(days=3)}, today) == \
        "closes " + clock.when(today + timedelta(days=3), today)


def test_alert_body_plain_language(monkeypatch):
    from datetime import timedelta
    from src import clock, watcher

    monkeypatch.setattr(watcher.db, "get_holdings",
                        lambda: [{"symbol": "INFY", "exchange": "NSE", "qty": 6,
                                  "buy_price": 1455.0}])

    rule = {"symbol": "INFY", "exchange": "NSE", "label": "below 200dma dip",
            "mode": "level", "conditions": [
                {"metric": "price_vs_ma200", "op": "<", "value": -10}]}
    values = {"price": 1169.0, "pct_change_day": -0.5, "price_vs_ma200": -11.2}
    ok, reasons, evaluable = watcher.evaluate_rule(rule, values)
    assert ok and evaluable
    assert reasons == ["the gap between price and its 200-day average is -11.2% "
                       "— under your -10.0% line"]

    body, html_body = watcher.alert_body(rule, values, reasons,
                                         clock.ist_today() - timedelta(days=2))
    assert clock.stamp()[:12] in body                  # dated, always
    assert "₹1,169" in body and "▼ -0.5% today" in body
    assert "You hold 6 shares at ₹1,455 average" in body
    assert "-₹1,716" in body                           # what it costs you today
    assert "3 days running" in body                    # explains the repeat
    assert "Pause it" in body
    # the HTML part says the same things, and colours each number for itself
    assert "₹1,169" in html_body and "-₹1,716" in html_body
    assert "below 200dma dip" in html_body and "3 days running" in html_body
    assert html_body.count("<script") == 0

    fresh, _ = watcher.alert_body(rule, values, reasons, clock.ist_today())
    assert "turned true today" in fresh and "days running" not in fresh
    # a crossing alert must not claim it will repeat daily
    edge, _ = watcher.alert_body({**rule, "mode": "edge"}, values, reasons)
    assert "One-off crossing" in edge and "running" not in edge
    # a missing holdings store must not stop the alert going out
    monkeypatch.setattr(watcher.db, "get_holdings",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert "just matched" in watcher.alert_body(rule, values, reasons)[0]


def test_level_rule_mails_once_a_day(monkeypatch):
    from datetime import timedelta, timezone
    from src import clock, watcher

    now = clock.ist_now()
    assert watcher._fired_today(now.isoformat()) is True
    assert watcher._fired_today((now - timedelta(days=1)).isoformat()) is False
    assert watcher._fired_today(None) is False
    # stored timestamps are UTC; the same instant read as IST must still be today
    assert watcher._fired_today(now.astimezone(timezone.utc).isoformat()) is True

    # and the run loop honours it: a standing rule that already mailed today
    # stays quiet even though its condition is still true
    rule = {"id": 1, "symbol": "INFY", "exchange": "NSE", "label": "dip",
            "mode": "level", "last_state": 1,
            "true_since": (clock.ist_today() - timedelta(days=3)).isoformat(),
            "last_triggered": now.astimezone(timezone.utc).isoformat(),
            "conditions": [{"metric": "price", "op": "<", "value": 2000}]}
    sent = []
    monkeypatch.setattr(watcher.db, "init_db", lambda: None)
    monkeypatch.setattr(watcher.db, "set_last_state", lambda *a: None)
    monkeypatch.setattr(watcher.db, "set_true_since", lambda *a: None)
    monkeypatch.setattr(watcher.db, "mark_triggered", lambda *a: None)
    monkeypatch.setattr(watcher.db, "log_alert", lambda *a, **k: None)
    monkeypatch.setattr(watcher.db, "get_holdings", lambda: [])
    monkeypatch.setattr(watcher, "gather_values",
                        lambda s, e: {"price": 1169.0, "pct_change_day": -0.5})
    monkeypatch.setattr(watcher.alerts, "dispatch",
                        lambda s, b, channels=None, html_body=None:
                        sent.append((s, html_body)) or ["email"])

    monkeypatch.setattr(watcher.db, "get_rules", lambda active_only=True: [rule])
    assert watcher.run_once(verbose=False) == [] and sent == []

    # yesterday's mail doesn't hold it back — one a day, not one ever
    stale = {**rule, "last_triggered": (now - timedelta(days=1)).astimezone(
        timezone.utc).isoformat()}
    monkeypatch.setattr(watcher.db, "get_rules", lambda active_only=True: [stale])
    assert len(watcher.run_once(verbose=False)) == 1
    assert sent and sent[0][0].startswith("🔔 INFY · dip · ")
    assert "<table" in sent[0][1]          # the mail carries an HTML part too


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


def test_shop_scoring_and_new_stores():
    from src import shop

    # relevance: filler results score low, real matches high
    assert shop.relevance("one piece anime printed t-shirt",
                          "Men Solid Round Neck Cotton Blend T-Shirt") < 0.34
    assert shop.relevance("one piece anime printed t-shirt",
                          "Free Authority Men One Piece Anime Graphic Printed Tshirt") > 0.7
    # plural/singular slack
    assert shop.relevance("running shoe", "Nice Running Shoes for Men") == 1.0

    # the composite score orders like a sane buyer would
    proven = {"title": "ASIAN white running shoes", "price": 569,
              "mrp": 1499, "rating": 4.0, "reviews": 112087}
    thin = {"title": "ASIAN white running shoes", "price": 999,
            "mrp": None, "rating": 4.6, "reviews": 12}
    junk = {"title": "green casual sandal", "price": 300,
            "mrp": 4999, "rating": 3.4, "reviews": 400}
    q = "white running shoes"
    s1, s2, s3 = (shop.score(r, q, 1000) for r in (proven, thin, junk))
    assert s1 > s2 > s3
    assert 0 <= s3 and s1 <= 100

    # review-count shorthand used by the jina fallback
    assert shop._count("8.8K") == 8800
    assert shop._count("1.1L") == 110000
    assert shop._count("(19)") == 19
    assert shop._count("") is None

    # myntra: products come embedded as window.__myx JSON
    myx = ('<script>window.__myx = {"searchData":{"results":{"products":['
           '{"productId":1,"product":"Puma Smashic Sneakers","productName":'
           '"Puma Smashic Sneakers","brand":"Puma","price":950,"mrp":4499,'
           '"rating":4.33209,"ratingCount":64015,'
           '"landingPageUrl":"casual-shoes/puma/1/buy"}]}}};</script>')
    rows = shop._parse_myntra(myx)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Puma Smashic Sneakers"      # brand not doubled
    assert r["price"] == 950 and r["rating"] == 4.3 and r["reviews"] == 64015
    assert r["url"].endswith("casual-shoes/puma/1/buy")
    assert shop._parse_myntra("<html>no payload</html>") == []
    assert shop._parse_myntra(myx, max_price=800) == []

    # amazon jina-markdown fallback parser
    md = ("## SPARX\n"
          "## [Sports Shoe SM-171 for Men](https://www.amazon.in/Sparx-White/dp/B07YP2F21T/ref=sr_1_2?x=1)\n"
          "4.1[_4.1 out of 5 stars_](javascript:void(0))[(8.8K)](https://www.amazon.in/x)\n"
          "300+ bought in past month\n"
          "Price, product page[₹988₹988 M.R.P: ₹1,499 M.R.P: ₹1,499₹1,499](https://www.amazon.in/x)\n")
    rows = shop._parse_amazon_md(md)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "SPARX Sports Shoe SM-171 for Men"
    assert r["price"] == 988 and r["mrp"] == 1499
    assert r["rating"] == 4.1 and r["reviews"] == 8800
    assert r["url"] == "https://www.amazon.in/Sparx-White/dp/B07YP2F21T"
    assert shop._parse_amazon_md("", None) == []

    assert "myntra.com" in shop.search_urls("white shoes")["Myntra"]

    # keepa chart helpers (only Amazon has a public history source)
    assert shop.asin("https://www.amazon.in/Sparx-White/dp/B07YP2F21T") == "B07YP2F21T"
    assert shop.asin("https://www.flipkart.com/x/p/itm123") is None
    assert "asin=B07YP2F21T" in shop.keepa_png("B07YP2F21T")
    assert "domain=in" in shop.keepa_png("B07YP2F21T")


def test_shop_watch_tracking(tmp_path, monkeypatch):
    from datetime import date, timedelta

    monkeypatch.setenv("STOCKWATCH_STATE_KEY", "k")
    monkeypatch.setattr(repo_state, "STATE_DIR", tmp_path)
    from src import shop_watch as sw
    monkeypatch.setattr(sw, "WATCH_JSON", tmp_path / "shop_watch.json")
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)

    assert sw.add("ASIAN Wonder-13 white", "https://a.in/dp/B1", "Amazon", 569)
    assert sw.add("Bushirt One Piece tee", "https://f.com/p/itm2", "Flipkart", 469)
    assert sw.add("dupe", "https://a.in/dp/B1", "Amazon", 999)   # same url ignored
    rows = sw.load()
    assert len(rows) == 2 and rows[0]["history"][0]["p"] == 569

    # add() seeds a point dated the real today, so the "next days" must be
    # relative — hardcoded dates made this test rot in a day
    day2 = (date.today() + timedelta(days=1)).isoformat()
    day3 = (date.today() + timedelta(days=2)).isoformat()
    day4 = (date.today() + timedelta(days=3)).isoformat()

    # day 2: one price drops 5% (alert), the other holds (silent)
    prices = {"https://a.in/dp/B1": 540, "https://f.com/p/itm2": 469}
    items, alerts = sw.check_all(fetch=lambda u, s: prices[u], today=day2)
    assert len(alerts) == 1 and "540" in alerts[0] and "new low" in alerts[0]
    assert len(items[0]["history"]) == 2

    # same-day re-check refreshes the point instead of stacking a second one
    items, _ = sw.check_all(fetch=lambda u, s: prices[u], today=day2)
    assert len(items[0]["history"]) == 2

    # a store that doesn't answer just skips the day
    items, alerts = sw.check_all(fetch=lambda u, s: None, today=day3)
    assert alerts == [] and len(items[0]["history"]) == 2

    # target hit beats the % rule
    rows = sw.load()
    rows[0]["target"] = 500
    sw.save(rows)
    _, alerts = sw.check_all(fetch=lambda u, s: 499 if "B1" in u else 469,
                             today=day4)
    assert len(alerts) == 1 and "target" in alerts[0]

    assert sw.remove("https://a.in/dp/B1")
    assert len(sw.load()) == 1
