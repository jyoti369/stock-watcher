"""Streamlit dashboard: watchlist, data-driven suggestions, deep per-stock
analysis (fundamentals + peers + valuation history + Monte Carlo + backtest), alerts.

Run from the project root:
    ./.venv/bin/streamlit run dashboard.py
"""
from __future__ import annotations

import os
import subprocess
import threading
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

from src import (advice, advisor_bot, ai_insights, alerts, analysis, bearcase,
                 datasource, db, finance_plan, fundamentals, gh_sync, importer,
                 mf, portfolio, projection, reminders, repo_state, scan_history,
                 sectors, suggestions, verdict, watcher)
from src.config import DATA_DIR

SUGG_CACHE = DATA_DIR / "suggestions_cache.pkl"

st.set_page_config(page_title="Stock Watcher", page_icon="📈", layout="wide")

# --- update feel -----------------------------------------------------------
# Streamlit's default is to grey out the whole page and spin a "running" badge
# on every rerun, which makes a one-tap checkbox look like a page load. Modern
# apps update in place instead. Two halves to that: heavy writes moved off the
# click path (see auto_sync below) and this, which stops the visual flicker —
# stale content stays fully visible and readable while the new value arrives.
st.markdown("""
<style>
  [data-testid="stStatusWidget"] { display: none !important; }
  [data-testid="stAppDeployButton"] { display: none !important; }
  .stApp [data-stale="true"], .stApp .element-container[data-stale="true"] {
      opacity: 1 !important; transition: none !important; filter: none !important; }
  [data-testid="stAppViewContainer"] { transition: none !important; }
  /* toasts read as the confirmation, so make them easy to catch */
  [data-testid="stToast"] { font-size: 0.95rem; }
</style>
""", unsafe_allow_html=True)


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
                        "state/holdings.json", "state/suggestions_history.json",
                        "state/finance_plan.json", "state/mf_holdings.json",
                        "state/advice.json", "state/reminders.json"],
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


# Background sync: the local export is instant (disk), but the GitHub push is a
# network round-trip that used to run inline on every tap — that wait WAS the
# lag you felt. Now the click returns as soon as state hits disk and the push
# happens on a worker thread. _SYNC_LOCK serialises pushes (git can't run two at
# once) and _sync_status carries the result back for the sidebar to show.
_SYNC_LOCK = threading.Lock()
_sync_status: dict[str, str] = {"state": "idle", "msg": ""}


def _push_worker() -> None:
    """Runs off the UI thread. Never touches st.* — only the status dict."""
    with _SYNC_LOCK:
        _sync_status.update(state="syncing", msg="")
        ok, msg = sync_to_github()
        if not ok and gh_sync.available():
            ok, msg = gh_sync.push_state()
        _sync_status.update(state="ok" if ok else "failed", msg=msg)


def auto_sync() -> None:
    """Save state now, push to GitHub in the background.

    Export is synchronous so what's on disk always matches what you just did
    (and a later manual sync can't lose it). The push is fire-and-forget, so no
    interaction ever blocks on the network.
    """
    repo_state.export_config()
    if st.session_state.get("_autosync_dead"):
        return
    if _sync_status.get("state") == "failed":
        # one hard failure is usually config (no token/remote); stop retrying on
        # every tap and let the sidebar button handle it explicitly
        st.session_state["_autosync_dead"] = True
        return
    threading.Thread(target=_push_worker, daemon=True).start()


def monte_carlo_block(symbol, exchange, years, amount, period_label):
    """Shared probability-range renderer. Plain-first: the odds verdict leads,
    the quant labels ride along in brackets so users learn the terms."""
    hist = datasource.get_history(symbol, exchange)
    mc = projection.monte_carlo(hist, years, amount)
    if not mc:
        st.caption("Not enough price history for a projection.")
        return
    p = mc["prob_profit"]
    if p >= 60:
        odds = f"🟢 The odds lean **in your favour** — {p:.0f}% of scenarios ended in profit."
    elif p >= 45:
        odds = f"🟡 **Roughly a coin flip** — {p:.0f}% of scenarios ended in profit."
    else:
        odds = f"🔴 The odds lean **against you** — only {p:.0f}% of scenarios ended in profit."
    st.markdown(odds)
    d, m, u = st.columns(3)
    d.metric("If it goes badly", inr(mc["p10_end"]), f"{mc['p10_ret']:+.0f}%",
             help="The worst 10% of simulated outcomes ended at or below this.")
    m.metric("Middle of the road", inr(mc["median_end"]), f"{mc['median_ret']:+.0f}%",
             help="The median — half the simulations ended above this, half below. "
                  "The most honest single guess.")
    u.metric("If it goes well", inr(mc["p90_end"]), f"{mc['p90_ret']:+.0f}%",
             help="The best 10% of simulated outcomes ended at or above this.")
    st.caption(
        f"How to read this: we replayed this stock's own past daily moves {mc['sims']:,} times "
        f"('Monte Carlo simulation') to see where {inr(amount)} could land in ~{period_label}. "
        f"{mc['prob_loss20']:.0f}% of runs lost more than 20%. "
        "It shows the *range* of realistic outcomes — it does not predict which one you'll get.")


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
        st.session_state["_autosync_dead"] = False
        with st.spinner("Pushing…"):
            ok, msg = sync_to_github()
            if not ok and gh_sync.available():
                ok, msg = gh_sync.push_state()
        _sync_status.update(state="ok" if ok else "failed", msg=msg)
        st.toast(("✅ " if ok else "⚠️ ") + msg)
    _ss = _sync_status.get("state")
    if _ss == "syncing":
        st.caption("🔄 Saving to GitHub in the background…")
    elif _ss == "failed":
        st.caption(f"⚠️ Last sync failed: {_sync_status.get('msg', '')[:80]} — "
                   "changes are saved locally; hit the button to retry.")
    elif _ss == "ok":
        st.caption(f"🟢 Saved to GitHub · {_sync_status.get('msg', '')[:60]}")
    elif gh_sync.available():
        st.caption("🟢 Changes auto-save to GitHub (works from your phone too).")
    else:
        st.caption("Auto-sync works locally via git. To make changes from the hosted app "
                   "stick too, add a STOCKWATCH_GH_TOKEN secret (see DEPLOY.md).")


def _warm_caches(pairs: list[tuple[str, str]]) -> None:
    """Fetch all symbols' data in parallel once, so the per-tab loops below hit
    warm caches instead of doing ~30 sequential network round-trips per rerun."""
    pairs = list(dict.fromkeys(pairs))
    if len(pairs) < 2:
        return
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
            list(ex.map(lambda p: watcher.gather_values(p[0], p[1]), pairs))
    except Exception:
        pass


# --- fragments -------------------------------------------------------------
# A plain st.rerun() re-executes this whole file — all eight tab bodies, every
# price loop — just to flip one checkbox. @st.fragment reruns ONLY the function
# below, so these interactions feel instant and the rest of the page never
# blinks. Each fragment reloads the data it owns, so it stays self-consistent.

@st.fragment
def plan_body_fragment() -> None:
    """The plan markdown + its tappable checklist, updating in place."""
    plan = finance_plan.load_plan()
    if plan is None:
        st.info("No plan saved yet. Write it below and hit Save.")
        return
    st.caption(f"Last updated: {plan['updated']} · stored encrypted in the repo, "
               "readable only on devices holding your key")
    st.markdown(plan["content"])

    # Guarded so a transient deploy hiccup here can't white-screen every tab
    # (Streamlit runs all tab bodies on each render).
    try:
        items = finance_plan.checklist_items(plan["content"])
    except AttributeError:
        st.caption("Checklist is loading a fresh deploy — reload in a moment.")
        return
    if not items:
        return
    done_n = sum(1 for it in items if it["done"])
    with st.expander(f"✅ Checklist — tap to mark done ({done_n}/{len(items)})",
                     expanded=done_n < len(items)):
        st.caption("Tapping a box updates the plan directly and syncs — no AI in "
                   "between, so what you see is what's saved. Untick to reopen.")
        for it in items:
            new = st.checkbox(it["text"], value=it["done"], key=f"plan_chk_{it['line']}")
            if new != it["done"]:
                updated = finance_plan.set_check(plan["content"], it["line"], new)
                if finance_plan.save_plan(updated):
                    auto_sync()
                    st.toast(("Done: " if new else "Reopened: ") + it["text"][:40])
                    st.rerun(scope="fragment")


@st.fragment
def reminders_fragment() -> None:
    """Reminder list + add/done/remove, all without a full-page rerun."""
    from datetime import date as _rdate
    st.markdown("### 📅 Reminders")
    rlist = reminders.load() or []
    _rtoday = _rdate.today()
    rsorted = sorted(rlist, key=lambda r: (reminders.effective_date(r, _rtoday) or _rdate.max))
    due_now = [r for r in rsorted if reminders.due(r, _rtoday)]
    if due_now:
        st.info("⏰ **Due now:** " + " · ".join(
            f"{r['text']} ({advice.pretty_date(reminders.effective_date(r, _rtoday).isoformat())})"
            for r in due_now))
    if rsorted:
        for r in rsorted:
            eff = reminders.effective_date(r, _rtoday)
            when = advice.pretty_date(eff.isoformat()) if eff else "?"
            tag = " · every year" if r.get("yearly") else ""
            flag = " ⏰" if reminders.due(r, _rtoday) else ""
            done = " · ✅ done" if r.get("done") and not r.get("yearly") else ""
            st.markdown(f"- **{when}**{tag} — {r['text']}{flag}{done}")
    else:
        st.caption("No reminders yet. Add one below (e.g. the April PPF top-up).")

    with st.expander("➕ Add / manage reminders"):
        with st.form("add_reminder", clear_on_submit=True):
            rc1, rc2, rc3 = st.columns([3, 1.3, 1])
            r_text = rc1.text_input("What to remember", placeholder="Deposit ₹1.5L into PPF")
            r_date = rc2.date_input("Date", format="DD/MM/YYYY")
            r_yearly = rc3.checkbox("Every year")
            if st.form_submit_button("Add reminder") and r_text.strip():
                rlist.append(reminders.new(r_text.strip(), r_date.isoformat(), r_yearly))
                if reminders.save(rlist):
                    auto_sync()
                    st.toast("Reminder added")
                    st.rerun(scope="fragment")
        if rsorted:
            st.caption("Remove or mark a one-off done:")
            for i, r in enumerate(rsorted):
                q1, q2, q3 = st.columns([4, 1, 1])
                eff = reminders.effective_date(r, _rtoday)
                q1.write(f"{advice.pretty_date(eff.isoformat()) if eff else '?'} — {r['text']}")
                if not r.get("yearly") and q2.button("Done", key=f"rem_done_{i}"):
                    r["done"] = True
                    reminders.save(rlist)
                    auto_sync()
                    st.rerun(scope="fragment")
                if q3.button("Remove", key=f"rem_del_{i}"):
                    rlist.remove(r)
                    reminders.save(rlist)
                    auto_sync()
                    st.rerun(scope="fragment")


QUICK_ASKS = [
    "How is my whole portfolio doing?",
    "What is my worst holding and should I still hold it?",
    "Is my plan on track this month?",
    "What should I do this week?",
]


def _render_actions(msg: dict, mi: int) -> None:
    """Confirm buttons for anything the advisor offered to set up.

    Nothing is written until one of these is tapped — the model only ever
    proposes, `advisor_bot.apply_action` does the actual write in plain Python.
    """
    actions = msg.get("actions") or []
    if not actions:
        return
    applied = msg.setdefault("applied", {})
    st.caption("The advisor can set these up for you:")
    for ai_, a in enumerate(actions):
        key = str(ai_)
        line = advisor_bot.describe_action(a)
        if key in applied:
            st.markdown(f"✅ {line}  \n_{applied[key]}_")
            continue
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"• {line}" + (f"  \n_{a.get('why')}_" if a.get("why") else ""))
        if c2.button("Confirm", key=f"adv_act_{mi}_{ai_}", type="primary"):
            ok, res = advisor_bot.apply_action(a)
            if ok:
                applied[key] = res
                auto_sync()
                st.toast("✅ " + res)
            else:
                st.toast("⚠️ " + res)
            st.rerun(scope="fragment")


def _ask_advisor(q: str, chat: list) -> None:
    st.chat_message("user").markdown(q)
    with st.chat_message("assistant"):
        with st.spinner("reading your data, pulling live prices + news…"):
            res = advisor_bot.answer(q, chat)
    chat.append({"role": "user", "content": q})
    if res.get("error"):
        st.error(res["error"])
    else:
        chat.append({"role": "assistant", "content": res["text"],
                     "actions": res.get("actions") or [],
                     "sources": res.get("sources") or [],
                     "engine": res.get("engine", "")})


@st.fragment
def advisor_fragment() -> None:
    """The chat. A fragment so asking a question doesn't re-run every tab."""
    st.caption("🔒 Your data stays in the app; only the question plus the relevant "
               "figures are sent to your AI key for that one answer. It can set up "
               "reminders, alerts and ledger calls, but only after you tap confirm.")
    chat = st.session_state.setdefault("advisor_chat", [])
    cc1, cc2 = st.columns([4, 1])
    cc1.caption(f"{sum(1 for m in chat if m['role'] == 'user')} question(s) this session")
    if cc2.button("Clear chat", key="adv_chat_clear") and chat:
        st.session_state["advisor_chat"] = []
        st.rerun(scope="fragment")

    if not chat:
        st.caption("Quick starts:")
        qcols = st.columns(len(QUICK_ASKS))
        for i, qa in enumerate(QUICK_ASKS):
            if qcols[i].button(qa, key=f"adv_quick_{i}", width="stretch"):
                _ask_advisor(qa, chat)
                st.rerun(scope="fragment")

    for mi, m in enumerate(chat):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] == "assistant":
                if m.get("engine"):
                    st.caption(m["engine"])
                _render_actions(m, mi)
                if m.get("sources"):
                    with st.expander("sources it read"):
                        for n in m["sources"]:
                            st.markdown(f"- [{n['title']}]({n['url']}) · {n.get('date','')}")

    if q := st.chat_input("Ask about a holding, the plan, or a what-if…"):
        _ask_advisor(q, chat)
        st.rerun(scope="fragment")


watchlist = db.get_watchlist()
_warm_caches([(w["symbol"], w["exchange"]) for w in watchlist]
             + [(h["symbol"], h["exchange"]) for h in db.get_holdings()]
             + [(r["symbol"], r["exchange"]) for r in db.get_rules(active_only=False)])
tabs = st.tabs(["📋 Overview", "💼 Portfolio", "💡 Suggestions",
                "🔍 Stock analysis", "🔔 Alerts", "🗺️ Plan", "🧭 Advice",
                "💬 Advisor"])

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
                with st.spinner("Reading file & resolving symbols…"):
                    fdf, err = importer.read_any_excel(f, f.name, password=fpw or None)
                    rows = []
                    if not err:
                        rows, err = importer.parse_workbook(fdf)
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

    # ---------------------------------------------------------- mutual funds
    st.divider()
    st.subheader("🪙 Mutual funds")
    st.caption("Units × the **official AMFI NAV** (published daily by the MF industry "
               "body — same number your fund house reports). Rows still waiting for "
               "unit allotment show as estimates until units are filled in.")

    @st.cache_data(ttl=3600, show_spinner=False)
    def _nav(code: str):
        return mf.latest_nav(code)

    mf_rows = mf.load_mf()
    if not os.environ.get("STOCKWATCH_STATE_KEY"):
        st.warning("Set STOCKWATCH_STATE_KEY in secrets to unlock the MF portfolio — "
                   "it is stored encrypted so the public repo never sees it.")
    elif not mf_rows:
        st.info("No funds saved yet — add one below, or paste your Angel One MF page "
                "into the AI import.")
    else:
        vrows, missing_nav = [], 0
        for h in mf_rows:
            nav = _nav(str(h["code"])) if h.get("code") else None
            if h.get("code") and nav is None:
                missing_nav += 1
            vrows.append(mf.value_row(h, nav))
        tot_val = sum(r["value"] for r in vrows if r["value"])
        tot_inv = sum(r["invested"] for r in vrows if r["invested"])
        live_n = sum(1 for r in vrows if r["source"].startswith("live"))
        m1, m2, m3 = st.columns(3)
        m1.metric("MF value", inr(tot_val))
        m2.metric("Priced live (AMFI)", f"{live_n}/{len(vrows)} funds")
        m3.metric("Awaiting units/cost", f"{len(vrows) - live_n} rows")
        if missing_nav:
            st.warning(f"{missing_nav} fund(s) had no NAV this run — showing estimates.")
        def _mf_color(x):
            if isinstance(x, (int, float)) and not pd.isna(x):
                return "color: #4ade80" if x > 0 else "color: #fb7185" if x < 0 else ""
            return ""

        mdf = pd.DataFrame([{
            "Fund": r["name"], "Units": r["units"], "NAV ₹": r["nav"],
            "Value": r["value"], "Invested": r["invested"],
            "P&L": r["pnl"], "P&L %": r["pnl_pct"],
            "Priced": r["source"], "Note": r["note"] or "",
        } for r in vrows])
        st.dataframe(
            mdf.style.map(_mf_color, subset=["P&L", "P&L %"])
               .format({"Units": "{:,.3f}", "NAV ₹": "₹{:,.2f}", "Value": "₹{:,.0f}",
                        "Invested": "₹{:,.0f}", "P&L": "₹{:+,.0f}", "P&L %": "{:+.1f}%"},
                       na_rep="—"),
            width="stretch", hide_index=True)
        st.caption("P&L needs the invested amount — rows without it show value only. "
                   "The monthly CAS statement (CAMS/KFintech email) has exact units for "
                   "every fund you own anywhere; use it to replace the estimates.")

    if os.environ.get("STOCKWATCH_STATE_KEY"):
        with st.expander("➕ Add a fund (AMFI search)"):
            q = st.text_input("Scheme name", placeholder="parag parikh flexi",
                              key="mf_q")
            hits = mf.search_schemes(q) if len(q.strip()) >= 4 else []
            if q.strip() and not hits:
                st.caption("No match — try fewer/simpler words (the AMFI search is "
                           "picky, e.g. 'hdfc mid cap' not 'HDFC Mid-Cap Opportunities').")
            if hits:
                pick = st.selectbox("Scheme", hits[:25],
                                    format_func=lambda x: x["name"], key="mf_pick")
                a1, a2 = st.columns(2)
                m_units = a1.number_input("Units (0 if not yet allotted)",
                                          min_value=0.0, step=0.001, format="%.3f")
                m_inv = a2.number_input("Invested ₹ (0 if unknown)",
                                        min_value=0.0, step=1000.0)
                if st.button("Add fund", key="mf_add"):
                    rows = mf.load_mf() or []
                    rows.append({"name": pick["name"], "code": pick["code"],
                                 "units": m_units or None, "invested": m_inv or None,
                                 "est_value": m_inv or None, "note": None})
                    if mf.save_mf(rows):
                        auto_sync()
                        st.toast(f"Added {pick['name']}")
                        st.rerun()

        with st.expander("📋 Import MF portfolio with AI (paste from Angel One)"):
            st.caption("Angel One → Mutual funds → Portfolio: select-all/copy the page "
                       "text and paste. **Replaces the whole MF list** after preview.")
            mf_paste = st.text_area("Paste here", height=140, key="mf_paste",
                                    label_visibility="collapsed")
            if st.button("✨ Parse funds", key="mf_ai") and mf_paste.strip():
                with st.spinner("Reading your paste…"):
                    rows, err = mf.parse_mf_with_ai(mf_paste)
                if err:
                    st.error(err)
                else:
                    for r in rows:                     # match names to AMFI codes
                        hit = mf.search_schemes(r["name"])
                        if hit:
                            r["code"], r["name"] = hit[0]["code"], hit[0]["name"]
                    st.session_state["mf_preview"] = rows
                    st.rerun()
            mprev = st.session_state.get("mf_preview")
            if mprev:
                med = st.data_editor(pd.DataFrame(mprev), num_rows="dynamic",
                                     hide_index=True, key="mf_prev_ed")
                b1, b2 = st.columns(2)
                if b1.button(f"✅ Replace MF list with these {len(med)} funds",
                             type="primary", key="mf_go"):
                    good = [r for r in med.to_dict("records") if r.get("name")]
                    if mf.save_mf(good):
                        del st.session_state["mf_preview"]
                        auto_sync()
                        st.toast(f"Imported {len(good)} funds")
                        st.rerun()
                if b2.button("Cancel", key="mf_cancel"):
                    del st.session_state["mf_preview"]
                    st.rerun()

        if mf_rows:
            with st.expander("⚙️ Edit funds (units / invested / notes)"):
                st.caption("Fill **units** once an allotment lands (Angel One order "
                           "detail or the CAS email shows them) — the row flips from "
                           "estimate to live pricing.")
                ed = st.data_editor(pd.DataFrame(mf_rows), num_rows="dynamic",
                                    hide_index=True, key="mf_edit")
                if st.button("Save changes", key="mf_save"):
                    cleaned = []
                    for r in ed.to_dict("records"):
                        if not r.get("name"):
                            continue
                        for k in ("units", "invested", "est_value"):
                            v = r.get(k)
                            r[k] = None if (v is None or pd.isna(v) or v == 0) else float(v)
                        r["code"] = str(r["code"]) if r.get("code") and not pd.isna(r["code"]) else None
                        r["note"] = r.get("note") or None
                        cleaned.append(r)
                    if mf.save_mf(cleaned):
                        auto_sync()
                        st.toast("MF portfolio saved & synced")
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
                    st.caption(f"What the pros expect (12-month): {av['num_analysts'] or '?'} analysts rate it "
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
        st.markdown("**📈 What your money could become** (probabilistic projection)")
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
                               ("price_vs_ma50", "vs 50-day avg"), ("price_vs_ma200", "vs 200-day avg")]:
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
                if q[3].button("Below 200-day avg", key="qa4"):
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

# ==================================================================== plan
with tabs[5]:
    st.subheader("🗺️ Money plan")
    if not os.environ.get("STOCKWATCH_STATE_KEY"):
        st.warning("Set STOCKWATCH_STATE_KEY in secrets to unlock the plan — it is "
                   "stored encrypted so the public repo never sees it.")
    else:
        plan_body_fragment()
        st.divider()
        reminders_fragment()

        with st.expander("✏️ Edit plan"):
            draft = st.text_area("Markdown",
                                 value=(finance_plan.load_plan() or {}).get("content", ""),
                                 height=380, label_visibility="collapsed")
            if st.button("Save plan"):
                if finance_plan.save_plan(draft):
                    auto_sync()
                    st.toast("Plan saved & synced")
                    st.rerun()
                else:
                    st.error("Couldn't save — encryption key missing.")

# ================================================================== advice
with tabs[6]:
    st.subheader("🧭 Buy/sell advice ledger")
    st.caption("Each holding's standing call: the stance, the reason, a **catalyst** "
               "to watch for (and when), exit bands that arm as live alerts, and a "
               "review date. Closed calls keep their outcome — this is also the "
               "advisor's scoreboard. Judgment under uncertainty, not predictions.")
    if not os.environ.get("STOCKWATCH_STATE_KEY"):
        st.warning("Set STOCKWATCH_STATE_KEY in secrets to unlock the ledger — it is "
                   "stored encrypted so the public repo never sees it.")
    else:
        from datetime import date as _date
        adv = advice.load_advice() or []
        open_calls = [a for a in adv if a.get("status", "OPEN") == "OPEN"]
        closed = [a for a in adv if a.get("status", "OPEN") != "OPEN"]
        today = _date.today()
        due = [a for a in open_calls if advice.due_soon(a, today)]

        right = sum(1 for a in closed if a["status"] == "DONE-RIGHT")
        wrong = sum(1 for a in closed if a["status"] == "DONE-WRONG")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Open calls", len(open_calls))
        s2.metric("Due to review", len(due), "look now" if due else None)
        s3.metric("Closed right", right)
        s4.metric("Closed wrong", wrong)

        if due:
            st.info("⏰ **Time to look again:** " + ", ".join(
                f"{a['symbol']} ({advice.pretty_date(a.get('catalyst_date') or a.get('review_by'))})"
                for a in due))

        # arm every exit band as a watcher alert, idempotently
        ac1, ac2 = st.columns([1, 3])
        if ac1.button("🔔 Arm all exit alerts", key="adv_arm"):
            wanted = advice.alert_rules_from(open_calls)
            for r in db.get_rules(active_only=False):
                if advice.is_advice_rule(r.get("label")):
                    db.delete_rule(r["id"])
            for w in wanted:
                db.add_rule(w["symbol"], w["exchange"], w["label"],
                            w["conditions"], mode=w["mode"])
            repo_state.export_config()
            auto_sync()
            st.toast(f"Armed {len(wanted)} exit alert(s) in the watcher")
            st.rerun()
        ac2.caption("Creates/refreshes watcher price alerts from every open call's "
                    "sell-above / stop-below band. Safe to press again anytime — it "
                    "replaces the old advice alerts, never duplicates.")

        _ICON = {"KEEP": "🟢", "TRIM": "🟠", "SELL": "🔴", "HOLD-RULE": "🟡", "WATCH": "⚪"}
        _ORDER = {"SELL": 0, "TRIM": 1, "HOLD-RULE": 2, "WATCH": 3, "KEEP": 4}
        if not open_calls:
            st.info("No open calls yet — add them in the editor below.")
        for a in sorted(open_calls, key=lambda a: _ORDER.get(a.get("stance"), 9)):
            flag = " · ⏰ review due" if advice.due_soon(a, today) else ""
            with st.container(border=True):
                st.markdown(f"### {_ICON.get(a.get('stance'), '•')} {a['symbol']} "
                            f"— {a.get('stance', '')}{flag}")
                st.markdown(f"**Why:** {a.get('thesis', '')}")
                if a.get("catalyst"):
                    when = f" _(around {advice.pretty_date(a['catalyst_date'])})_" if a.get("catalyst_date") else ""
                    st.markdown(f"**Watch for:** {a['catalyst']}{when}")
                bands = []
                if advice._num(a.get("sell_above")):
                    bands.append(f"sell into strength above **₹{advice._num(a['sell_above']):g}**")
                if advice._num(a.get("stop_below")):
                    bands.append(f"stop out below **₹{advice._num(a['stop_below']):g}**")
                if bands:
                    st.markdown("**Exit bands:** " + " · ".join(bands))
                foot = []
                if a.get("review_by"):
                    foot.append(f"review by {advice.pretty_date(a['review_by'])}")
                if a.get("added"):
                    foot.append(f"since {advice.pretty_date(a['added'])}")
                if foot:
                    st.caption(" · ".join(foot))

        if closed:
            with st.expander(f"📜 Closed calls — scoreboard ({len(closed)})"):
                st.dataframe(pd.DataFrame([{
                    "Symbol": a["symbol"], "Stance": a["stance"], "Why": a["thesis"],
                    "Status": a["status"], "Outcome": a.get("outcome") or "—",
                    "Since": advice.pretty_date(a.get("added")),
                } for a in closed]), width="stretch", hide_index=True)

        with st.expander("✏️ Edit ledger (add / close / correct)"):
            st.caption("stance: KEEP · TRIM · SELL · HOLD-RULE · WATCH — "
                       "status: OPEN · DONE-RIGHT · DONE-WRONG · DONE-MOOT. "
                       "Close a call with an outcome note instead of deleting it. "
                       "Dates as YYYY-MM-DD; bands are numbers (blank = none).")
            aed = st.data_editor(
                pd.DataFrame(adv if adv else [advice.new_entry("", "WATCH", "")]),
                num_rows="dynamic", hide_index=True, key="adv_edit",
                column_config={
                    "stance": st.column_config.SelectboxColumn(options=advice.STANCES),
                    "status": st.column_config.SelectboxColumn(options=advice.STATUSES),
                    "sell_above": st.column_config.NumberColumn(format="%.2f"),
                    "stop_below": st.column_config.NumberColumn(format="%.2f"),
                })
            if st.button("Save ledger", key="adv_save"):
                good = []
                for r in aed.to_dict("records"):
                    if not r.get("symbol") or not str(r.get("symbol")).strip():
                        continue
                    r = dict(r)
                    for k in ("sell_above", "stop_below"):
                        r[k] = advice._num(r.get(k))
                    for k, v in list(r.items()):
                        if k in ("sell_above", "stop_below"):
                            continue
                        if v is None or (isinstance(v, float) and pd.isna(v)):
                            r[k] = ""
                    r["symbol"] = str(r["symbol"]).upper().strip()
                    good.append(r)
                if advice.save_advice(good):
                    auto_sync()
                    st.toast("Ledger saved & synced")
                    st.rerun()
                else:
                    st.error("Couldn't save — encryption key missing.")

# ================================================================= advisor
with tabs[7]:
    st.subheader("💬 Ask your advisor")
    st.caption("Knows your **whole app** — every holding with live P&L, your funds, "
               "plan, checklist, ledger, alerts and reminders — mixed with **live "
               "prices + fresh news** for whatever stock you ask about. It can also "
               "set up reminders, alerts and ledger calls for you, on your confirm.")
    if not ai_insights.available().get("gemini") and not ai_insights.available().get("openai"):
        st.warning("No AI key set — add STOCKWATCH_GEMINI_KEY in secrets to enable the chat.")
    elif not os.environ.get("STOCKWATCH_STATE_KEY"):
        st.warning("Set STOCKWATCH_STATE_KEY so the advisor can read your (encrypted) data.")
    else:
        advisor_fragment()
