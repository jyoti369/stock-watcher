"""Streamlit dashboard: watchlist, data-driven suggestions, deep per-stock
analysis (fundamentals + peers + valuation history + Monte Carlo + backtest), alerts.

Run from the project root:
    ./.venv/bin/streamlit run dashboard.py
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime

import pandas as pd
import streamlit as st

# On Streamlit Cloud, secrets live in st.secrets (not env). Bridge them to env
# BEFORE importing src.config so it picks them up. No-op locally / if unset.
try:
    for _k in ["STOCKWATCH_TG_TOKEN", "STOCKWATCH_TG_CHAT", "STOCKWATCH_SMTP_USER",
               "STOCKWATCH_SMTP_PASS", "STOCKWATCH_EMAIL_TO", "STOCKWATCH_APP_PASSWORD",
               "STOCKWATCH_GEMINI_KEY", "STOCKWATCH_OPENAI_KEY", "STOCKWATCH_GH_TOKEN",
               "STOCKWATCH_STATE_KEY"]:
        if _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

from src import (ai_insights, alerts, analysis, bearcase, datasource, db,
                 fundamentals, gh_sync, importer, portfolio, projection,
                 repo_state, scan_history, sectors, suggestions, verdict, watcher)
from src.config import DATA_DIR

SUGG_CACHE = DATA_DIR / "suggestions_cache.pkl"

st.set_page_config(page_title="Stock Watcher", page_icon="📈", layout="wide")


def _require_password() -> None:
    """Gate the app behind STOCKWATCH_APP_PASSWORD when it's set (e.g. on a public
    Streamlit Cloud URL). No password set = no gate, so local use is unaffected.

    After a correct entry we stamp a key into the URL (?k=…), so refreshes and
    bookmarks stay signed in — you type the password once per device, not per
    reload. Sharing that URL shares access, same as sharing the password."""
    import hashlib
    pw = os.environ.get("STOCKWATCH_APP_PASSWORD", "")
    if not pw:
        return
    key = hashlib.sha256(f"stockwatch:{pw}".encode()).hexdigest()[:20]
    if st.session_state.get("_authed") or st.query_params.get("k") == key:
        st.session_state["_authed"] = True
        if st.query_params.get("k") != key:
            st.query_params["k"] = key          # keep it in the URL for next refresh
        return
    st.markdown("### 🔒 Stock Watcher")
    entered = st.text_input("Password", type="password")
    if entered == pw:
        st.session_state["_authed"] = True
        st.query_params["k"] = key
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
        subprocess.run(["git", "add", "state/watchlist.json", "state/rules.json",
                        "state/holdings.json", "state/suggestions_history.json"],
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


def auto_sync() -> None:
    """Export state and quietly push it to GitHub so the 24/7 watcher stays current.
    Tries `git push` first (local); where that's impossible (Streamlit Cloud) it
    falls back to the GitHub API if a token is configured. Only gives up for the
    session when both paths fail — the manual sidebar button stays as fallback."""
    repo_state.export_config()
    if st.session_state.get("_autosync_dead"):
        return
    ok, _ = sync_to_github()
    if not ok and gh_sync.available():
        ok, _ = gh_sync.push_state()
    if not ok:
        st.session_state["_autosync_dead"] = True


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
                auto_sync()
                st.toast(f"Added {new_sym}")
                st.rerun()

    st.divider()
    st.caption("**Notifications**")
    for ch, ok in alerts.channel_status().items():
        st.write(f"{'🟢' if ok else '⚪'} {ch}{'' if ok else ' · off'}")
    if st.button("🔔 Run alert check now", width="stretch"):
        fired = watcher.run_once(verbose=False)
        st.toast(f"{len(fired)} alert(s) fired" if fired else "Checked — nothing triggered")

    if st.button("🔄 Refresh data now", width="stretch"):
        datasource._CACHE.clear()
        st.cache_data.clear()
        st.toast("Cleared cache — pulling fresh data")
        st.rerun()
    st.caption("Data caches ~15 min; refresh to force fresh prices/fundamentals.")

    if st.button("⬆️ Sync watchlist/rules to GitHub", width="stretch"):
        ok, msg = sync_to_github()
        if not ok and gh_sync.available():
            ok, msg = gh_sync.push_state()
        st.toast(("✅ " if ok else "⚠️ ") + msg)
    if gh_sync.available():
        st.caption("🟢 Changes auto-save to GitHub (works from your phone too).")
    else:
        st.caption("Auto-sync works locally via git. To make changes from the hosted app "
                   "stick too, add a STOCKWATCH_GH_TOKEN secret (see DEPLOY.md).")


watchlist = db.get_watchlist()
tabs = st.tabs(["📋 Overview", "💼 Portfolio", "💡 Suggestions",
                "🔍 Stock analysis", "🔔 Alerts"])

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
        df = pd.DataFrame(rows)

        def _pct_color(v):
            if isinstance(v, (int, float)):
                return "color: #4ade80" if v > 0 else "color: #fb7185" if v < 0 else ""
            return ""

        styled = (df.style
                  .map(_pct_color, subset=["Day %", "1Y %"])
                  .format({"Price": "₹{:,.2f}", "Day %": "{:+.2f}%", "1Y %": "{:+.1f}%",
                           "P/E": "{:.1f}", "ROE %": "{:.1f}%", "RSI": "{:.0f}"}, na_rep="—"))
        st.dataframe(styled, width="stretch", hide_index=True)
        st.caption("**Health** = share of fundamental checks passed: 65+ 🟢 OK · 40–64 🟡 Mixed · "
                   "<40 🔴 Weak — about the business, not the price. Open **Stock analysis** for "
                   "the deep view + bottom line. Prices via NSE live where available, else ~15-min delayed.")

        with st.expander("⚙️ Manage watchlist"):
            for w in watchlist:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{w['symbol']} · {w['exchange']} — {w.get('name','')}")
                if c2.button("Remove", key=f"rm_{w['symbol']}_{w['exchange']}"):
                    db.remove_from_watchlist(w["symbol"], w["exchange"])
                    auto_sync()
                    st.rerun()

# =============================================================== portfolio
with tabs[1]:
    st.subheader("💼 Portfolio")
    st.caption("What you actually hold, with live profit & loss. Add each buy below — "
               "the same stock bought twice shows as two lots.")
    if os.environ.get("STOCKWATCH_STATE_KEY"):
        st.caption("🔒 Your holdings are **encrypted** before syncing — the public repo "
                   "only ever sees ciphertext.")
    else:
        st.warning("⚠️ No STOCKWATCH_STATE_KEY set — holdings would sync in plaintext to "
                   "the public repo. Add the key (see secrets file) before importing real data.")

    with st.expander("➕ Add a holding", expanded=not db.get_holdings()):
        with st.form("add_holding", clear_on_submit=True):
            h1, h2, h3, h4, h5 = st.columns([1.4, 1, 1, 1, 1.2])
            h_sym = h1.text_input("Symbol", placeholder="TCS").strip().upper()
            h_exch = h2.selectbox("Exchange", ["NSE", "BSE"], key="h_exch")
            h_qty = h3.number_input("Qty", min_value=0.0, value=10.0, step=1.0)
            h_price = h4.number_input("Buy price (₹)", min_value=0.0, value=0.0, step=1.0)
            h_date = h5.date_input("Buy date", value=None, format="DD/MM/YYYY")
            if st.form_submit_button("Add holding") and h_sym and h_qty > 0 and h_price > 0:
                db.add_holding(h_sym, h_exch, h_qty, h_price,
                               h_date.isoformat() if h_date else None)
                auto_sync()
                st.toast(f"Added {h_qty:g} × {h_sym} @ ₹{h_price:g}")
                st.rerun()

    with st.expander("📥 Import from Angel One / any broker"):
        st.caption("Fastest way to fill this page: upload the holdings file your broker "
                   "gives you, or just paste the rows. You'll see a preview to check/fix "
                   "before anything is saved. **Importing replaces all current holdings** "
                   "(the statement is the whole truth).")
        up_tab, paste_tab = st.tabs(["📄 Upload file (CSV/Excel)", "📋 Paste rows"])

        with up_tab:
            st.caption("Angel One app/web → Portfolio/Reports → Holdings → download.")
            f = st.file_uploader("Holdings file", type=["csv", "xlsx", "xls"],
                                 label_visibility="collapsed")
            fpw = st.text_input("File password (only if the file is locked — usually your "
                                "PAN in capitals)", type="password", key="imp_pw")
            if f is not None and st.button("Read file", key="imp_read"):
                fdf, err = importer.read_any_excel(f, f.name, password=fpw or None)
                rows = []
                if not err:
                    rows, err = importer.parse_table(fdf)
                if err:
                    st.error(err)
                else:
                    st.session_state["import_preview"] = rows
                    st.rerun()

        with paste_tab:
            st.caption("One holding per line, e.g. `INFY 10 1450.50` — messy text is fine, "
                       "AI parsing handles it.")
            pasted = st.text_area("Paste here", height=140, label_visibility="collapsed",
                                  placeholder="INFY 10 1450.50\nTCS-EQ 5 3120\nCDSL 20 1150.25")
            pc1, pc2 = st.columns(2)
            if pc1.button("Parse", key="imp_parse") and pasted.strip():
                rows = importer.parse_text(pasted)
                if rows:
                    st.session_state["import_preview"] = rows
                    st.rerun()
                else:
                    st.warning("Couldn't parse that — try 'Parse with AI'.")
            if pc2.button("✨ Parse with AI", key="imp_ai") and pasted.strip():
                with st.spinner("Reading your paste…"):
                    rows, err = importer.parse_with_ai(pasted)
                if err:
                    st.error(err)
                else:
                    st.session_state["import_preview"] = rows
                    st.rerun()

        preview = st.session_state.get("import_preview")
        if preview:
            st.markdown(f"**Check these {len(preview)} holdings** — edit anything that's "
                        "off, then confirm:")
            edited = st.data_editor(
                pd.DataFrame(preview), num_rows="dynamic", hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("Symbol", required=True),
                    "qty": st.column_config.NumberColumn("Qty", min_value=0.0),
                    "buy_price": st.column_config.NumberColumn("Avg buy ₹", min_value=0.0),
                }, key="imp_editor")
            cc1, cc2 = st.columns(2)
            if cc1.button(f"✅ Replace my holdings with these {len(edited)} rows",
                          type="primary", key="imp_go"):
                good = [r for r in edited.to_dict("records")
                        if r.get("symbol") and (r.get("qty") or 0) > 0 and (r.get("buy_price") or 0) > 0]
                n = db.replace_holdings(good)
                del st.session_state["import_preview"]
                auto_sync()
                st.toast(f"Imported {n} holdings")
                st.rerun()
            if cc2.button("Cancel", key="imp_cancel"):
                del st.session_state["import_preview"]
                st.rerun()

    holdings = db.get_holdings()
    if not holdings:
        st.info("No holdings yet. **Import from your broker above** (30 seconds), or add "
                "one manually — then this page shows your live P&L, today's move, and "
                "each stock's health at a glance.")
    else:
        lot_rows, rows = [], []
        for h in holdings:
            v = watcher.gather_values(h["symbol"], h["exchange"])
            s = analysis.score_fundamentals(h["symbol"], h["exchange"])
            lot = portfolio.lot_row(h, v)
            lot_rows.append(lot)
            rows.append({
                "Symbol": lot["symbol"], "Qty": lot["qty"], "Buy ₹": lot["buy_price"],
                "Now ₹": lot["price"], "Day %": lot["day_pct"],
                "Invested": lot["invested"], "Value": lot["value"],
                "P&L": lot["pnl"], "P&L %": lot["pnl_pct"],
                "Health": RATING_BADGE.get(s.get("rating"), "⚪ —"),
            })

        tot = portfolio.totals(lot_rows)
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("Invested", inr(tot["invested"]))
        t2.metric("Current value", inr(tot["value"]),
                  f"{tot['pnl_pct']:+.1f}%" if tot["pnl_pct"] is not None else None)
        t3.metric("Total P&L", inr(tot["pnl"]), "profit" if tot["pnl"] >= 0 else "loss")
        t4.metric("Today", inr(tot["day_move"]),
                  f"{tot['day_pct']:+.2f}%" if tot["day_pct"] is not None else None)
        if tot["missing"]:
            st.warning(f"{tot['missing']} holding(s) had no live price this run — totals "
                       "exclude them. Hit 🔄 Refresh data in the sidebar.")

        pdf = pd.DataFrame(rows)

        def _pl_color(x):
            if isinstance(x, (int, float)):
                return "color: #4ade80" if x > 0 else "color: #fb7185" if x < 0 else ""
            return ""

        st.dataframe(
            pdf.style.map(_pl_color, subset=["Day %", "P&L", "P&L %"])
               .format({"Qty": "{:g}", "Buy ₹": "₹{:,.2f}", "Now ₹": "₹{:,.2f}",
                        "Day %": "{:+.2f}%", "Invested": "₹{:,.0f}", "Value": "₹{:,.0f}",
                        "P&L": "₹{:+,.0f}", "P&L %": "{:+.1f}%"}, na_rep="—"),
            width="stretch", hide_index=True)
        st.caption("P&L is vs your buy price (dividends not counted). Health = the business "
                   "quality read — open **Stock analysis** for the full picture + bottom line.")

        with st.expander("⚙️ Manage holdings"):
            for h in holdings:
                c1, c2 = st.columns([4, 1])
                bd = f" · bought {h['buy_date']}" if h.get("buy_date") else ""
                c1.write(f"{h['qty']:g} × **{h['symbol']}** @ ₹{h['buy_price']:g}{bd}")
                if c2.button("Remove", key=f"rmh_{h['id']}"):
                    db.remove_holding(h["id"])
                    auto_sync()
                    st.rerun()

# ============================================================= suggestions
with tabs[2]:
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
                ranked_now = suggestions.rank(uni, top_n=top_n)
                st.session_state["suggestions"] = ranked_now
                st.session_state["suggestions_ts"] = datetime.now().strftime("%d %b %Y, %H:%M")
                try:                       # persist so the next visit is instant
                    import pickle
                    SUGG_CACHE.write_bytes(pickle.dumps(
                        {"ts": st.session_state["suggestions_ts"], "rows": ranked_now}))
                except Exception:
                    pass
                # append to the permanent scan history (with stance one-liners)
                try:
                    stances = {r["symbol"]: verdict.build(
                        r["health"], r.get("metrics", {}),
                        (r.get("bear") or {}).get("valuation"), r.get("peer"))["stance"]
                        for r in ranked_now}
                    scan_history.append(
                        st.session_state["suggestions_ts"],
                        {"universe": universe_choice, "period": period_label,
                         "amount": amount, "top_n": top_n},
                        ranked_now, stances)
                    auto_sync()
                except Exception:
                    pass

    # no scan this session? show the last saved one instantly
    if "suggestions" not in st.session_state and SUGG_CACHE.exists():
        try:
            import pickle
            cached_scan = pickle.loads(SUGG_CACHE.read_bytes())
            st.session_state["suggestions"] = cached_scan["rows"]
            st.session_state["suggestions_ts"] = cached_scan["ts"]
        except Exception:
            pass

    ranked = st.session_state.get("suggestions", [])
    if ranked and st.session_state.get("suggestions_ts"):
        st.caption(f"Showing scan from **{st.session_state['suggestions_ts']}** — "
                   "prices/news may have moved since; hit **Find suggestions** to rescan.")
    if ranked:
        st.info("Candidates to research, **not** advice. Profit figures are probability "
                "ranges from past behaviour — never guaranteed. Check before you buy.")
        with st.expander("❓ What do these scores mean?"):
            st.markdown(
                "- **Opportunity (0–100)** — how well things line up *right now*, used to rank this "
                "list. Blend of: fundamental health 40% · upside to analysts' target 30% · trend vs "
                "200-day average 20% · not-overbought 10%. A ranking aid, not a buy signal.\n"
                "- **Health (0–100)** — how good the *business* is: the share of fundamental checks "
                "it passes (profitability, debt, cash flow, growth, earnings quality…). "
                "**65+ 🟢 OK · 40–64 🟡 Mixed · below 40 🔴 Weak.** Says nothing about price — "
                "a great business can still be expensive.\n"
                "- **Bottom line** — one honest sentence combining quality, valuation and trend.")

        for i, r in enumerate(ranked, 1):
            av = r["analyst"]
            rec = av["recommendation"] if av else "no coverage"
            hlth = r["health"]
            header = (f"#{i}  {r['symbol']} · {r['name'][:32]}  —  opportunity {r['score']}/100  "
                      f"·  health {hlth['rating']}  ·  {rec}")
            with st.expander(header, expanded=(i == 1)):
                # one-line bottom line, synthesised from the already-computed pieces
                v = verdict.build(hlth, r.get("metrics", {}),
                                  (r.get("bear") or {}).get("valuation"), r.get("peer"))
                st.markdown(f"📌 **{v['stance']}**")

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Opportunity", f"{r['score']}/100",
                          help="How well things line up right now (ranking aid, not a buy signal): "
                               "health 40% + analyst upside 30% + trend 20% + not-overbought 10%.")
                m2.metric("Deep health", RATING_BADGE.get(hlth["rating"], "—"),
                          f"{hlth.get('score')}/100" if hlth.get("score") is not None else None,
                          help="Share of fundamental checks passed. 65+ 🟢 OK · 40–64 🟡 Mixed · "
                               "<40 🔴 Weak. About the business, not the price.")
                m3.metric("Price", inr(r["price"]))
                if av:
                    m4.metric("Analyst target", inr(av["target"]), f"{av['upside_pct']:+.1f}%",
                              help="Average 12-month target of the analysts covering it, vs price now.")

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
        st.info("No stocks scored — the data source may be rate-limiting. "
                "Hit 🔄 Refresh data in the sidebar and try again.")
    else:
        st.caption("👆 Pick a universe, period and amount, then hit **Find suggestions**. "
                   "Takes ~30–60s — it scores every stock live, then deep-checks the top picks.")

    # ---------------- scan history: every past scan, and how its picks did
    past = scan_history.load()
    if past:
        st.markdown("---")
        st.markdown("#### 📜 Scan history")
        st.caption("Every scan is saved with the prices at that moment — open one and hit "
                   "**How did these do?** to see the return since. This keeps the engine honest.")
        for si, scan in enumerate(past):
            p = scan.get("params", {})
            n_picks = len(scan.get("picks", []))
            with st.expander(f"{scan['ts']} · {p.get('universe', '?')} · top {n_picks}"):
                perf_key = f"scanperf_{si}"
                show_perf = st.session_state.get(perf_key)
                if st.button("📈 How did these do?", key=f"perfbtn_{si}", disabled=bool(show_perf)):
                    perf = {}
                    with st.spinner("Fetching current prices…"):
                        for pick in scan["picks"]:
                            v = watcher.gather_values(pick["symbol"], "NSE")
                            perf[pick["symbol"]] = v.get("price")
                    st.session_state[perf_key] = perf
                    st.rerun()

                hrows = []
                for pick in scan["picks"]:
                    row = {"Symbol": pick["symbol"], "Score": pick.get("score"),
                           "Health": pick.get("health"),
                           "Price then": pick.get("price_then")}
                    if show_perf:
                        now = show_perf.get(pick["symbol"])
                        row["Price now"] = now
                        row["Since %"] = (round((now / pick["price_then"] - 1) * 100, 1)
                                          if (now and pick.get("price_then")) else None)
                    row["Bottom line (then)"] = (pick.get("stance") or "")[:70]
                    hrows.append(row)
                hdf = pd.DataFrame(hrows)
                if show_perf and "Since %" in hdf:
                    st.dataframe(
                        hdf.style.map(
                            lambda x: ("color: #4ade80" if isinstance(x, (int, float)) and x > 0
                                       else "color: #fb7185" if isinstance(x, (int, float)) and x < 0
                                       else ""), subset=["Since %"])
                           .format({"Price then": "₹{:,.0f}", "Price now": "₹{:,.0f}",
                                    "Since %": "{:+.1f}%", "Score": "{:.0f}"}, na_rep="—"),
                        width="stretch", hide_index=True)
                else:
                    st.dataframe(hdf, width="stretch", hide_index=True)
        if st.button("🗑️ Clear history"):
            scan_history.clear()
            auto_sync()
            st.rerun()

# ============================================================ stock detail
with tabs[3]:
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
        val = bearcase.valuation_percentile(picked_sym, picked_exch)
        peer = sectors.peer_comparison(picked_sym, picked_exch)

        st.markdown(f"### {score.get('name', picked_sym)}  ·  {picked_sym}")
        if score.get("sector"):
            st.caption(f"Sector: {score['sector']}")

        with st.expander("❓ New here? How to read this page"):
            st.markdown(
                "- **Health (0–100)** — the share of fundamental checks the company passes "
                "(profitability, debt, cash flow, growth, earnings quality…): "
                "**65+ 🟢 OK · 40–64 🟡 Mixed · below 40 🔴 Weak**. It's about the *business*, "
                "not the price — a healthy company can still be expensive.\n"
                "- **Valuation vs history / peers** — is the P/E high or low vs its own past and its "
                "sector? High = a lot of optimism already priced in.\n"
                "- **Bear case** — the honest 'what could go wrong', from the numbers.\n"
                "- **Probabilistic projection** — a range of outcomes from simulating its own past "
                "moves, with the odds. Not a prediction.\n"
                "- **Signal backtest** — did a trading rule actually work on this stock historically?\n"
                "- **AI live insight** — a summary of recent news, with sources.\n"
                "- **Bottom line** (at the end) — all of it in one plain takeaway.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Price", inr(vals.get("price")),
                  f"{vals['pct_change_day']:+.2f}%" if vals.get("pct_change_day") is not None else None)
        c2.metric("Deep health", RATING_BADGE.get(score.get("rating"), "—"),
                  f"{score.get('score')}/100" if score.get("score") is not None else None,
                  help="Share of fundamental checks passed (profitability, debt, cash flow, growth, "
                       "earnings quality…). 65+ 🟢 OK · 40–64 🟡 Mixed · below 40 🔴 Weak. "
                       "Scores the business, not the price — a healthy company can still be "
                       "overpriced. See the Bottom line for what it means together.")
        c3.metric("P/E", f"{vals['pe']:.1f}" if vals.get("pe") else "—",
                  help="Price ÷ earnings per share. Higher = the market expects more growth.")
        c4.metric("1Y return", f"{vals['ret_1y']:+.1f}%" if vals.get("ret_1y") is not None else "—")

        # event/ownership signals
        extra = fundamentals.extra_signals(picked_sym, picked_exch)
        f_full = datasource.get_fundamentals(picked_sym, picked_exch)
        bits = []
        if extra.get("earnings_date"):
            bits.append(f"📅 Next earnings: **{extra['earnings_date']}**")
        if f_full.get("heldPercentInstitutions") is not None:
            bits.append(f"🏛️ Institutions {f_full['heldPercentInstitutions'] * 100:.0f}%")
        if f_full.get("heldPercentInsiders") is not None:
            bits.append(f"👤 Insiders/promoters {f_full['heldPercentInsiders'] * 100:.0f}%")
        if bits:
            st.caption(" · ".join(bits))
        if extra.get("rating_changes"):
            with st.expander("Recent analyst rating changes"):
                for rc in extra["rating_changes"]:
                    st.write(f"• {rc['date']} — {rc['firm']}: {rc['action']} {rc['from']} → {rc['to']}")

        if hist is not None and not hist.empty:
            span = st.radio("Range", ["1M", "2M", "3M", "6M", "1Y", "3Y", "Max"],
                            horizontal=True, index=4)
            n = {"1M": 21, "2M": 42, "3M": 63, "6M": 126, "1Y": 252,
                 "3Y": 756, "Max": len(hist)}[span]
            # compute moving averages on the FULL series, then slice — so the MA
            # lines are still correct even on a 1-month view
            close_full = hist["Close"]
            chart = pd.DataFrame({"Close": close_full,
                                  "MA50": close_full.rolling(50).mean(),
                                  "MA200": close_full.rolling(200).mean()}).tail(n)
            st.line_chart(chart)
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

        # valuation vs its own history (computed once, up top)
        if val:
            st.markdown("**Valuation vs its own 5-year history**")
            st.progress(min(val["percentile"], 100) / 100)
            st.caption(f"P/E (on reported annual EPS) {val['current_pe']} is at the "
                       f"**{val['percentile']}th percentile** of its own range "
                       f"({val['min_pe']}–{val['max_pe']}, median {val['median_pe']}) — {val['verdict']}. "
                       f"This is a different lens from the headline trailing P/E above.")

        # peers (computed once, up top)
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

        # AI live insight — web-grounded, cited
        st.markdown("**🤖 Live insight (web-grounded)**")
        ai_avail = ai_insights.available()
        if not (ai_avail["gemini"] or ai_avail["openai"]):
            st.caption("Add a Gemini or OpenAI key in config to enable this.")
        else:
            engines = ([("Gemini (free)", "gemini")] if ai_avail["gemini"] else []) + \
                      ([("OpenAI (paid)", "openai")] if ai_avail["openai"] else [])
            ec1, ec2 = st.columns([2, 3])
            eng = ec1.selectbox("Engine", engines, format_func=lambda e: e[0], key="ai_engine")
            if ec2.button("Generate live insight", key="ai_gen"):
                with st.spinner("Searching news & summarizing…"):
                    ctx = (f"price {vals.get('price')}, P/E {vals.get('pe')}, "
                           f"health {score.get('rating')}, 1Y {vals.get('ret_1y')}%")
                    st.session_state["ai_result"] = {
                        "symbol": picked_sym,
                        "res": ai_insights.generate(picked_sym, ctx, score.get("name"), engine=eng[1])}
            cached = st.session_state.get("ai_result")
            if cached and cached.get("symbol") == picked_sym:
                res = cached["res"]
                if not res:
                    st.caption("No AI engine available.")
                elif res.get("error"):
                    st.warning(res["error"])
                else:
                    st.write(res["text"])
                    if res.get("sources"):
                        st.caption("Sources: " + " · ".join(
                            f"[{i + 1}]({s['url']})" for i, s in enumerate(res["sources"][:6]) if s.get("url")))
                    st.caption(f"via {res['engine']} — a summary of public news, not advice.")

        # probabilistic projection
        st.markdown("**📈 Probabilistic projection**")
        pc1, pc2 = st.columns(2)
        p_period = pc1.selectbox("Period", list(PERIODS.keys()), index=2, key="an_period")
        p_amount = pc2.number_input("Amount (₹)", min_value=1000, value=100000, step=10000, key="an_amt")
        monte_carlo_block(picked_sym, picked_exch, PERIODS[p_period], p_amount, p_period)

        # backtest
        st.markdown("**🔬 Signal backtest** — did a rule actually work on this stock?")
        sig = st.selectbox(
            "Signal", list(projection.PRESETS.keys()),
            help="A 'signal' is a classic buy-timing trigger. This replays it across years of this "
                 "stock's history and shows what returns actually followed. "
                 "RSI oversold (<30) = beaten-down bounce setups · Dip 10% below 50-day avg = pullback "
                 "buys · Golden cross = when the 50-day average crosses above the 200-day (a trend "
                 "turning up). If the 'avg after signal' beats the any-day average with a high win "
                 "rate, the signal has had an edge on this stock.")
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

        # bottom line — plain synthesis of everything above
        st.markdown("---")
        v = verdict.build(score, vals, val, peer)
        st.markdown("### 📌 Bottom line")
        st.markdown(f"**{v['stance']}**")
        for p in v["points"]:
            st.markdown(f"- {p}")
        st.markdown(f"**What would make it more interesting:** {v['watch']}")
        st.caption("⚠️ " + v["caveat"] + " " + score.get("disclaimer", ""))

# ================================================================= alerts
with tabs[4]:
    st.subheader("🔔 Alert rules")
    st.caption("Pick a stock, see its live numbers, and add an alert in one click. "
               "Alerts ping your Telegram + email 24/7.")

    with st.expander("➕ New alert", expanded=not db.get_rules(active_only=False)):
        ac1, ac2 = st.columns([2, 1])
        wl_opts = [w["symbol"] for w in watchlist]
        typed = ac1.text_input("Symbol", placeholder="type any, e.g. CDSL").strip().upper()
        a_sym = typed
        if not typed and wl_opts:
            pick = ac1.selectbox("…or pick from watchlist", ["—"] + wl_opts, key="al_pick")
            a_sym = "" if pick == "—" else pick
        a_exch = ac2.selectbox("Exchange", ["NSE", "BSE"], key="al_exch")

        def _make(label, conditions, mode="edge"):
            db.add_rule(a_sym, a_exch, label, conditions, mode=mode)
            auto_sync()
            st.toast(f"Alert added — {a_sym}: {label}")
            st.rerun()

        if not a_sym:
            st.caption("Type or pick a symbol to see its current numbers and add alerts in one click.")
        else:
            snap = watcher.gather_values(a_sym, a_exch)
            price = snap.get("price")
            if price is None:
                st.warning(f"Couldn't fetch data for {a_sym} — check the symbol/exchange.")
            else:
                st.markdown(f"**{a_sym} right now** — set alerts off these:")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Price", inr(price))
                m2.metric("Day", f"{snap['pct_change_day']:+.1f}%" if snap.get("pct_change_day") is not None else "—")
                m3.metric("RSI", f"{snap['rsi14']:.0f}" if snap.get("rsi14") is not None else "—")
                m4.metric("P/E", f"{snap['pe']:.1f}" if snap.get("pe") else "—")
                extras = []
                for k, lbl in [("ret_1w", "1w"), ("ret_1m", "1m"), ("ret_1y", "1y"),
                               ("price_vs_ma50", "vs 50-DMA"), ("price_vs_ma200", "vs 200-DMA")]:
                    if snap.get(k) is not None:
                        extras.append(f"{lbl} {snap[k]:+.1f}%")
                if extras:
                    st.caption(" · ".join(extras))

                st.markdown("**One-click alerts** (fire once when it happens)")
                q = st.columns(4)
                if q[0].button("📉 Down 3% in a day", key="qa1"):
                    _make("down 3% in a day", [{"metric": "pct_change_day", "op": "<", "value": -3}])
                if q[1].button("RSI oversold <30", key="qa2"):
                    _make("RSI oversold (<30)", [{"metric": "rsi14", "op": "<", "value": 30}])
                if q[2].button("RSI overbought >70", key="qa3"):
                    _make("RSI overbought (>70)", [{"metric": "rsi14", "op": ">", "value": 70}])
                if q[3].button("Below 200-DMA", key="qa4"):
                    _make("below 200-day avg", [{"metric": "price_vs_ma200", "op": "<", "value": 0}])

                st.markdown("**Price target** (pre-filled ±5% from now — just tweak)")
                t1, t2 = st.columns(2)
                lo = t1.number_input("Alert if price falls below ₹", value=float(round(price * 0.95)),
                                     step=1.0, key="tgt_lo")
                if t1.button("Add drop alert", key="tgt_lo_b"):
                    _make(f"price below ₹{lo:.0f}", [{"metric": "price", "op": "<", "value": lo}])
                hi = t2.number_input("Alert if price rises above ₹", value=float(round(price * 1.05)),
                                     step=1.0, key="tgt_hi")
                if t2.button("Add rise alert", key="tgt_hi_b"):
                    _make(f"price above ₹{hi:.0f}", [{"metric": "price", "op": ">", "value": hi}])

                with st.expander("Advanced — custom multi-condition rule"):
                    with st.form("add_rule_custom", clear_on_submit=True):
                        r_label = st.text_input("Label", placeholder="cheap dip to buy-watch")
                        mode_label = st.radio("When to fire",
                                              ["Only when it crosses in (edge)", "Every check while true (level)"])
                        r_mode = "edge" if mode_label.startswith("Only") else "level"
                        keys = list(watcher.METRICS.keys())
                        conditions = []
                        for i in range(3):
                            cc1, cc2, cc3 = st.columns([3, 1, 2])
                            met = cc1.selectbox(f"Metric {i + 1}", ["—"] + keys,
                                                format_func=lambda k: watcher.METRICS.get(k, k), key=f"met_{i}")
                            op = cc2.selectbox("Op", list(watcher.OPS.keys()), key=f"op_{i}")
                            dv = cc3.number_input("Value", value=0.0, step=1.0, key=f"val_{i}")
                            if met != "—":
                                conditions.append({"metric": met, "op": op, "value": dv})
                        if st.form_submit_button("Create rule") and conditions:
                            db.add_rule(a_sym, a_exch, r_label or "alert", conditions, mode=r_mode)
                            auto_sync()
                            st.toast(f"Rule created for {a_sym}")
                            st.rerun()

    rules = db.get_rules(active_only=False)
    if rules:
        st.caption("**Pause** silences an alert without deleting it; **Resume** turns it back on. "
                   "The line under each rule shows how close it is to firing right now.")
    _rule_vals: dict[str, dict] = {}
    for rule in rules:
        cond_txt = " AND ".join(
            f"{watcher.METRICS.get(c['metric'], c['metric'])} {c['op']} {c['value']}"
            for c in rule["conditions"])
        active = bool(rule["active"])
        mode_tag = " · ⚡ edge" if rule.get("mode") == "edge" else ""
        status = "🟢 Active" if active else "⏸️ Paused"

        # near-fire preview: current value of each condition vs its target
        vkey = f"{rule['symbol']}:{rule['exchange']}"
        if vkey not in _rule_vals:
            _rule_vals[vkey] = watcher.gather_values(rule["symbol"], rule["exchange"])
        vals_now = _rule_vals[vkey]
        parts, n_met = [], 0
        for c in rule["conditions"]:
            cur = vals_now.get(c["metric"])
            label = watcher.METRICS.get(c["metric"], c["metric"])
            if cur is None:
                parts.append(f"{label}: no data")
                continue
            met = watcher.OPS[c["op"]](cur, float(c["value"]))
            n_met += met
            gap = abs(cur - float(c["value"]))
            state_txt = "✓ met" if met else f"needs {c['op']} {c['value']:g}, off by {gap:g}"
            parts.append(f"{label} is {cur:g} ({state_txt})")
        n_cond = len(rule["conditions"])
        prox = "🔥 firing" if n_met == n_cond else f"{n_met}/{n_cond} conditions met"

        cols = st.columns([5, 1, 1])
        cols[0].write(f"{status} · **{rule['symbol']}** — {rule.get('label')}{mode_tag}  \n{cond_txt}")
        cols[0].caption(f"Now: {prox} — " + "; ".join(parts))
        if cols[1].button("Pause" if active else "Resume", key=f"tog_{rule['id']}"):
            db.set_rule_active(rule["id"], not active)
            auto_sync()
            st.toast(("Paused" if active else "Resumed") + f" — {rule['symbol']}")
            st.rerun()
        if cols[2].button("Delete", key=f"del_{rule['id']}"):
            db.delete_rule(rule["id"])
            auto_sync()
            st.toast(f"Deleted — {rule['symbol']}")
            st.rerun()
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
