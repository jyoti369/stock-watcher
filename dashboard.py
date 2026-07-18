"""Streamlit dashboard: watchlist, data-driven suggestions, deep per-stock
analysis (fundamentals + peers + valuation history + Monte Carlo + backtest), alerts.

Run from the project root:
    ./.venv/bin/streamlit run dashboard.py
"""
from __future__ import annotations

import os
import subprocess

import pandas as pd
import streamlit as st

# On Streamlit Cloud, secrets live in st.secrets (not env). Bridge them to env
# BEFORE importing src.config so it picks them up. No-op locally / if unset.
try:
    for _k in ["STOCKWATCH_TG_TOKEN", "STOCKWATCH_TG_CHAT", "STOCKWATCH_SMTP_USER",
               "STOCKWATCH_SMTP_PASS", "STOCKWATCH_EMAIL_TO", "STOCKWATCH_APP_PASSWORD"]:
        if _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

from src import (alerts, analysis, bearcase, datasource, db, projection,
                 repo_state, sectors, suggestions, watcher)

st.set_page_config(page_title="Stock Watcher", page_icon="📈", layout="wide")


def _require_password() -> None:
    """Gate the app behind STOCKWATCH_APP_PASSWORD when it's set (e.g. on a public
    Streamlit Cloud URL). No password set = no gate, so local use is unaffected."""
    pw = os.environ.get("STOCKWATCH_APP_PASSWORD", "")
    if not pw or st.session_state.get("_authed"):
        return
    st.markdown("### 🔒 Stock Watcher")
    entered = st.text_input("Password", type="password")
    if entered == pw:
        st.session_state["_authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()


_require_password()
db.init_db()

# fresh cloud container has an empty db — seed watchlist/rules from committed state
if not db.get_watchlist() and repo_state.WATCHLIST_JSON.exists():
    try:
        repo_state.import_from_repo()
    except Exception:
        pass

RATING_BADGE = {"OK": "🟢 OK", "Mixed": "🟡 Mixed", "Weak": "🔴 Weak", "Unknown": "⚪ —"}
STATUS_ICON = {"good": "🟢", "ok": "🟡", "weak": "🔴", "info": "ℹ️"}
PERIODS = {"3 months": 0.25, "6 months": 0.5, "1 year": 1.0, "3 years": 3.0, "5 years": 5.0}


def inr(v) -> str:
    return f"₹{v:,.0f}" if isinstance(v, (int, float)) else "—"


def sync_to_github() -> tuple[bool, str]:
    """Commit the state/*.json (watchlist + rules) and push, so the GitHub Actions
    alert watcher picks them up. Pull --rebase first so the Action's cooldown
    commits (different files) merge cleanly."""
    repo_state.export_config()
    try:
        subprocess.run(["git", "add", "state/watchlist.json", "state/rules.json"],
                       check=True, cwd=str(repo_state.ROOT), capture_output=True)
        r = subprocess.run(["git", "commit", "-m", "update watchlist/rules"],
                           cwd=str(repo_state.ROOT), capture_output=True, text=True)
        if "nothing to commit" in (r.stdout + r.stderr):
            return True, "already up to date"
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       check=True, cwd=str(repo_state.ROOT), capture_output=True)
        subprocess.run(["git", "push", "origin", "main"],
                       check=True, cwd=str(repo_state.ROOT), capture_output=True)
        return True, "pushed to GitHub"
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or b"").decode()[:200] if isinstance(e.stderr, bytes) else str(e)


def monte_carlo_block(symbol, exchange, years, amount, period_label):
    """Shared Monte Carlo renderer for a stock."""
    hist = datasource.get_history(symbol, exchange)
    mc = projection.monte_carlo(hist, years, amount)
    if not mc:
        st.caption("Not enough price history for a projection.")
        return
    d, m, u = st.columns(3)
    d.metric(f"Downside (worst 10%)", inr(mc["p10_end"]), f"{mc['p10_ret']:+.0f}%")
    m.metric(f"Median outcome", inr(mc["median_end"]), f"{mc['median_ret']:+.0f}%")
    u.metric(f"Upside (best 10%)", inr(mc["p90_end"]), f"{mc['p90_ret']:+.0f}%")
    st.caption(
        f"{inr(amount)} held ~{period_label}: **{mc['prob_profit']:.0f}% chance of a profit**, "
        f"{mc['prob_loss20']:.0f}% chance of losing more than 20%. "
        f"From {mc['sims']:,} simulations resampling this stock's own past daily moves — "
        "a range of possibilities, not a prediction.")


# ================================================================ sidebar
with st.sidebar:
    st.title("📈 Stock Watcher")
    st.caption("Indian equities · NSE / BSE · free data")

    with st.expander("➕ Add to watchlist", expanded=True):
        with st.form("add_symbol", clear_on_submit=True):
            new_sym = st.text_input("Symbol", placeholder="TCS, INFY, RELIANCE…").strip().upper()
            new_exch = st.selectbox("Exchange", ["NSE", "BSE"])
            if st.form_submit_button("Add", width="stretch") and new_sym:
                db.add_to_watchlist(new_sym, new_exch, datasource.resolve_name(new_sym, new_exch))
                repo_state.export_config()
                st.toast(f"Added {new_sym}")
                st.rerun()

    st.divider()
    st.caption("**Notifications**")
    for ch, ok in alerts.channel_status().items():
        st.write(f"{'🟢' if ok else '⚪'} {ch}{'' if ok else ' · off'}")
    if st.button("🔔 Run alert check now", width="stretch"):
        fired = watcher.run_once(verbose=False)
        st.toast(f"{len(fired)} alert(s) fired" if fired else "Checked — nothing triggered")

    if st.button("⬆️ Sync watchlist/rules to GitHub", width="stretch"):
        ok, msg = sync_to_github()
        st.toast(("✅ " if ok else "⚠️ ") + msg)
    st.caption("Sync so the 24/7 GitHub Actions watcher sees your latest watchlist & rules.")


watchlist = db.get_watchlist()
tabs = st.tabs(["📋 Overview", "💡 Suggestions", "🔍 Stock analysis", "🔔 Alerts"])

# ================================================================ overview
with tabs[0]:
    st.subheader("Your watchlist")
    if not watchlist:
        st.info("Watchlist is empty — add a symbol from the sidebar (try TCS, INFY, RELIANCE).")
    else:
        rows = []
        for w in watchlist:
            v = watcher.gather_values(w["symbol"], w["exchange"])
            s = analysis.score_fundamentals(w["symbol"], w["exchange"])   # light = fast
            rows.append({
                "Symbol": w["symbol"], "Name": (w.get("name") or "")[:26],
                "Price": v.get("price"), "Day %": v.get("pct_change_day"),
                "1Y %": v.get("ret_1y"), "P/E": v.get("pe"), "ROE %": v.get("roe"),
                "RSI": v.get("rsi14"), "Health": RATING_BADGE.get(s.get("rating"), "⚪ —"),
            })
        st.dataframe(
            pd.DataFrame(rows), width="stretch", hide_index=True,
            column_config={
                "Price": st.column_config.NumberColumn(format="₹%.2f"),
                "Day %": st.column_config.NumberColumn(format="%.2f%%"),
                "1Y %": st.column_config.NumberColumn(format="%.1f%%"),
                "P/E": st.column_config.NumberColumn(format="%.1f"),
                "ROE %": st.column_config.NumberColumn(format="%.1f%%"),
                "RSI": st.column_config.NumberColumn(format="%.0f"),
            })
        st.caption("Health here is the quick read. Open **Stock analysis** for the deep, "
                   "statement-based view. Prices via NSE live where available, else ~15-min delayed.")

        with st.expander("⚙️ Manage watchlist"):
            for w in watchlist:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{w['symbol']} · {w['exchange']} — {w.get('name','')}")
                if c2.button("Remove", key=f"rm_{w['symbol']}_{w['exchange']}"):
                    db.remove_from_watchlist(w["symbol"], w["exchange"])
                    repo_state.export_config()
                    st.rerun()

# ============================================================= suggestions
with tabs[1]:
    st.subheader("💡 Suggestions")
    st.caption("Ranked by an opportunity score from **real data** — fundamental health, "
               "distance below analysts' target, and trend. Each pick is then deep-checked "
               "(statements, peers, bear case) so you see the reasons *for and against*.")

    c1, c2, c3, c4 = st.columns([1.4, 1, 1, 0.8])
    universe_choice = c1.radio("Scan", ["Popular large-caps", "My watchlist", "Both"])
    period_label = c2.selectbox("Holding period", list(PERIODS.keys()), index=2)
    amount = c3.number_input("Amount (₹)", min_value=1000, value=100000, step=10000)
    top_n = c4.slider("Show", 3, 10, 5)
    years = PERIODS[period_label]

    if st.button("🔍 Find suggestions", type="primary"):
        uni = []
        if universe_choice in ("My watchlist", "Both"):
            uni += [w["symbol"] for w in watchlist]
        if universe_choice in ("Popular large-caps", "Both"):
            uni += suggestions.DEFAULT_UNIVERSE
        if not uni:
            st.warning("Your watchlist is empty — pick 'Popular large-caps' or 'Both'.")
        else:
            with st.spinner(f"Scoring {len(set(uni))} stocks, then deep-checking the top {top_n}…"):
                st.session_state["suggestions"] = suggestions.rank(uni, top_n=top_n)

    ranked = st.session_state.get("suggestions", [])
    if ranked:
        st.info("Candidates to research, **not** advice. Profit figures are probability "
                "ranges from past behaviour — never guaranteed. Check before you buy.")

        for i, r in enumerate(ranked, 1):
            av = r["analyst"]
            rec = av["recommendation"] if av else "no coverage"
            hlth = r["health"]
            header = (f"#{i}  {r['symbol']} · {r['name'][:32]}  —  opportunity {r['score']}/100  "
                      f"·  health {hlth['rating']}  ·  {rec}")
            with st.expander(header, expanded=(i == 1)):
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Opportunity", f"{r['score']}/100")
                m2.metric("Deep health", RATING_BADGE.get(hlth["rating"], "—"),
                          f"{hlth.get('score')}/100" if hlth.get("score") is not None else None)
                m3.metric("Price", inr(r["price"]))
                if av:
                    m4.metric("Analyst target", inr(av["target"]), f"{av['upside_pct']:+.1f}%")

                st.markdown(f"**✅ Why it's on the list** — sector: {r.get('sector') or '—'}")
                for c in hlth.get("checks", []):
                    if c["status"] in ("good", "ok"):
                        st.write(f"{STATUS_ICON.get(c['status'])} {c['name']} — {c['detail']}")

                bear = r.get("bear", {})
                st.markdown("**⚠️ Why NOT / risks**")
                for f in bear.get("flags", []):
                    st.write(f"• {f}")

                if r.get("peer"):
                    pv = r["peer"]["verdict"]
                    bits = [v for v in pv.values()]
                    if bits:
                        st.markdown(f"**🏷️ Vs {r['peer']['group']} peers** — " + "; ".join(bits) + ".")

                st.markdown(f"**📈 What you might make · {period_label} · {inr(amount)}**")
                monte_carlo_block(r["symbol"], r["exchange"], years, amount, period_label)
                if av:
                    st.caption(f"Analyst 12-month view: {av['num_analysts'] or '?'} analysts rate it "
                               f"*{av['recommendation']}*, mean target {inr(av['target'])} "
                               f"(range {inr(av['low'])}–{inr(av['high'])}).")

                news = datasource.get_news(r["symbol"], r["exchange"], limit=3)
                if news:
                    st.markdown("**📰 Recent news**")
                    for n in news:
                        meta = " · ".join(x for x in [n.get("publisher"), n.get("date")] if x)
                        st.write(f"• {n['title']}" + (f"  \n  _{meta}_" if meta else ""))
    elif "suggestions" in st.session_state:
        st.info("No stocks scored — try a different universe.")

# ============================================================ stock detail
with tabs[2]:
    st.subheader("🔍 Stock analysis")
    options = [f"{w['symbol']} · {w['exchange']}" for w in watchlist]
    manual = st.text_input("Type any symbol", placeholder="e.g. HDFCBANK").strip().upper()
    picked_sym = picked_exch = None
    if manual:
        picked_sym, picked_exch = manual, "NSE"
    elif options:
        picked_sym, picked_exch = st.selectbox("Or pick from watchlist", options).split(" · ")

    if picked_sym:
        score = analysis.score_fundamentals(picked_sym, picked_exch, deep=True)
        vals = watcher.gather_values(picked_sym, picked_exch)
        hist = datasource.get_history(picked_sym, picked_exch)

        st.markdown(f"### {score.get('name', picked_sym)}  ·  {picked_sym}")
        if score.get("sector"):
            st.caption(f"Sector: {score['sector']}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Price", inr(vals.get("price")),
                  f"{vals['pct_change_day']:+.2f}%" if vals.get("pct_change_day") is not None else None)
        c2.metric("Deep health", RATING_BADGE.get(score.get("rating"), "—"),
                  f"{score.get('score')}/100" if score.get("score") is not None else None)
        c3.metric("P/E", f"{vals['pe']:.1f}" if vals.get("pe") else "—")
        c4.metric("1Y return", f"{vals['ret_1y']:+.1f}%" if vals.get("ret_1y") is not None else "—")

        if hist is not None and not hist.empty:
            span = st.radio("Range", ["6M", "1Y", "3Y", "Max"], horizontal=True, index=1)
            n = {"6M": 126, "1Y": 252, "3Y": 756, "Max": len(hist)}[span]
            h = hist.tail(n)
            st.line_chart(pd.DataFrame({"Close": h["Close"],
                                        "MA50": h["Close"].rolling(50).mean(),
                                        "MA200": h["Close"].rolling(200).mean()}))
            st.caption("Prices are dividend-adjusted (total return), so historical values, "
                       "returns and 52-week range may read differently from raw price charts elsewhere.")

        left, right = st.columns([3, 2])
        with left:
            st.markdown("**Fundamental scorecard** (statement-based)")
            for c in score.get("checks", []):
                st.write(f"{STATUS_ICON.get(c['status'], '•')} **{c['name']}** — {c['detail']}")
            if not score.get("checks"):
                st.info("Fundamental data wasn't available.")
        with right:
            st.markdown("**Trend**")
            tr = score.get("trend", {})
            st.write(f"Direction: {tr.get('direction', '—')}")
            st.write(f"RSI: {tr.get('rsi', '—')}")
            st.markdown("**History**")
            for k, v in (score.get("history_context") or {}).items():
                st.write(f"{k.replace('_', ' ').title()}: {v}")

        # valuation vs its own history
        val = bearcase.valuation_percentile(picked_sym, picked_exch)
        if val:
            st.markdown("**Valuation vs its own 5-year history**")
            st.progress(min(val["percentile"], 100) / 100)
            st.caption(f"P/E (on reported annual EPS) {val['current_pe']} is at the "
                       f"**{val['percentile']}th percentile** of its own range "
                       f"({val['min_pe']}–{val['max_pe']}, median {val['median_pe']}) — {val['verdict']}. "
                       f"This is a different lens from the headline trailing P/E above.")

        # peers
        peer = sectors.peer_comparison(picked_sym, picked_exch)
        if peer:
            st.markdown(f"**Peer comparison · {peer['group']}**")
            df = pd.DataFrame(peer["peers"]).rename(columns={
                "symbol": "Symbol", "pe": "P/E", "roe": "ROE %",
                "net_margin": "Net margin %", "rev_growth": "Rev growth %"})
            st.dataframe(df, width="stretch", hide_index=True)
            if peer["verdict"]:
                st.caption("vs peers: " + "; ".join(peer["verdict"].values()) + ".")

        # bear case
        bear = bearcase.bear_case(picked_sym, picked_exch)
        st.markdown("**⚠️ Bear case — what could go wrong**")
        for f in bear["flags"]:
            st.write(f"• {f}")

        # probabilistic projection
        st.markdown("**📈 Probabilistic projection**")
        pc1, pc2 = st.columns(2)
        p_period = pc1.selectbox("Period", list(PERIODS.keys()), index=2, key="an_period")
        p_amount = pc2.number_input("Amount (₹)", min_value=1000, value=100000, step=10000, key="an_amt")
        monte_carlo_block(picked_sym, picked_exch, PERIODS[p_period], p_amount, p_period)

        # backtest
        st.markdown("**🔬 Signal backtest** — did a rule actually work on this stock?")
        sig = st.selectbox("Signal", list(projection.PRESETS.keys()))
        bt = projection.backtest(hist, sig)
        if bt:
            st.write(f"Fired **{bt['num_signals']}** times over ~{bt['years']}y. "
                     "Average return AFTER the signal vs buying on any random day:")
            st.dataframe(pd.DataFrame(bt["results"]).rename(columns={
                "horizon": "Held for", "avg_return": "Avg after signal %",
                "win_rate": "Win rate %", "baseline": "Any-day avg %"}),
                width="stretch", hide_index=True)
            st.caption("If 'avg after signal' beats 'any-day avg' with a high win rate, the signal "
                       "has had an edge historically — past results, no guarantee of future ones.")
        else:
            st.caption("Not enough history to backtest this signal.")

        st.caption("⚠️ " + score.get("disclaimer", ""))

# ================================================================= alerts
with tabs[3]:
    st.subheader("🔔 Alert rules")
    st.caption("A rule fires when ALL its conditions hold — e.g. P/E below 25 AND today down 3%.")

    with st.expander("➕ New rule", expanded=not db.get_rules(active_only=False)):
        with st.form("add_rule", clear_on_submit=True):
            rc1, rc2, rc3 = st.columns([1, 1, 2])
            r_sym = rc1.text_input("Symbol", placeholder="TCS").strip().upper()
            r_exch = rc2.selectbox("Exchange", ["NSE", "BSE"], key="rule_exch")
            r_label = rc3.text_input("Label", placeholder="cheap dip to buy-watch")
            mode_label = st.radio(
                "When to fire",
                ["Every check while true (level)", "Only when it crosses into true (edge)"],
                help="Edge = alert once when the condition first becomes true, not repeatedly while it stays true.")
            r_mode = "edge" if mode_label.startswith("Only") else "level"
            keys = list(watcher.METRICS.keys())
            conditions = []
            for i in range(3):
                cc1, cc2, cc3 = st.columns([3, 1, 2])
                met = cc1.selectbox(f"Metric {i+1}", ["—"] + keys,
                                    format_func=lambda k: watcher.METRICS.get(k, k), key=f"met_{i}")
                op = cc2.selectbox("Op", list(watcher.OPS.keys()), key=f"op_{i}")
                val = cc3.number_input("Value", value=0.0, step=1.0, key=f"val_{i}")
                if met != "—":
                    conditions.append({"metric": met, "op": op, "value": val})
            if st.form_submit_button("Create rule") and r_sym and conditions:
                db.add_rule(r_sym, r_exch, r_label or "alert", conditions, mode=r_mode)
                repo_state.export_config()
                st.toast(f"Rule created for {r_sym}")
                st.rerun()

    rules = db.get_rules(active_only=False)
    for rule in rules:
        cond_txt = " AND ".join(
            f"{watcher.METRICS.get(c['metric'], c['metric'])} {c['op']} {c['value']}"
            for c in rule["conditions"])
        cols = st.columns([4, 1, 1])
        mode_tag = " · ⚡edge" if rule.get("mode") == "edge" else ""
        cols[0].write(f"{'🟢' if rule['active'] else '⏸️'} **{rule['symbol']}** — {rule.get('label')}{mode_tag}  \n{cond_txt}")
        if cols[1].button("Toggle", key=f"tog_{rule['id']}"):
            db.set_rule_active(rule["id"], not rule["active"]); repo_state.export_config(); st.rerun()
        if cols[2].button("Delete", key=f"del_{rule['id']}"):
            db.delete_rule(rule["id"]); repo_state.export_config(); st.rerun()
    if not rules:
        st.info("No rules yet.")

    st.markdown("**Recent alerts**")
    history = db.get_alert_history(limit=25)
    if history:
        st.dataframe(pd.DataFrame([{"When": h["ts"][:16], "Symbol": h["symbol"],
                                    "Message": h["message"], "Sent to": h["channels"]}
                                   for h in history]),
                     width="stretch", hide_index=True)
    else:
        st.info("No alerts have fired yet.")
