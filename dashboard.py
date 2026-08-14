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
                 clock, datasource, db, finance_plan, fmt, fundamentals,
                 gh_sync, importer, insights, ipo, mf, portfolio, projection,
                 reminders, repo_state, scan_history, sectors, settings, shop,
                 shop_watch, suggestions, verdict, watcher)
from src.config import DATA_DIR

SUGG_CACHE = DATA_DIR / "suggestions_cache.pkl"

st.set_page_config(page_title="Stock Watcher", page_icon="📈", layout="wide")

# --- update feel -----------------------------------------------------------
# Streamlit's default is to grey out the whole page on every rerun, which makes
# a one-tap checkbox look like a page load. So stale content stays fully
# visible and readable while the new value arrives.
#
# What it must NOT do is hide the fact that something is happening — this used
# to also hide stStatusWidget, and with no spinner and no grey-out a slow tap
# looked like the app had ignored you. The badge is back, restyled as a small
# "working" pill, and slow paths below get their own inline spinner or a
# shimmer placeholder so the waiting is visible where the data will appear.
st.markdown("""
<style>
  [data-testid="stAppDeployButton"] { display: none !important; }
  .stApp [data-stale="true"], .stApp .element-container[data-stale="true"] {
      opacity: 1 !important; transition: none !important; filter: none !important; }
  [data-testid="stAppViewContainer"] { transition: none !important; }
  /* the running badge: small, calm, and unmistakably "busy" */
  [data-testid="stStatusWidget"] {
      background: rgba(23,163,152,.14) !important; border: 1px solid #17a398 !important;
      border-radius: 20px !important; padding: 2px 10px 2px 6px !important;
      box-shadow: none !important; }
  [data-testid="stStatusWidget"] label, [data-testid="stStatusWidget"] div {
      color: #17a398 !important; font-size: .8rem !important; }
  /* toasts read as the confirmation, so make them easy to catch */
  [data-testid="stToast"] { font-size: 0.95rem; }
  /* shimmer used by skeleton() while a slow block is being fetched */
  .sw-shimmer { border-radius: 6px; margin: 7px 0;
      background: linear-gradient(90deg, rgba(148,163,184,.10) 25%,
        rgba(148,163,184,.28) 37%, rgba(148,163,184,.10) 63%);
      background-size: 400% 100%; animation: sw-sweep 1.2s ease-in-out infinite; }
  @keyframes sw-sweep { 0% { background-position: 100% 0 }
                        100% { background-position: 0 0 } }
</style>
""", unsafe_allow_html=True)


def skeleton(rows: int = 5, head: bool = True) -> str:
    """Shimmer bars shaped like the content that's coming.

    Used with a placeholder so the wait shows up exactly where the data will
    land, instead of a spinner somewhere else on the page:

        ph = st.empty()
        ph.markdown(skeleton(6), unsafe_allow_html=True)
        rows = slow_fetch()
        ph.empty()
    """
    bars = []
    if head:
        bars.append('<div class="sw-shimmer" style="height:26px;width:38%"></div>')
    for i in range(rows):
        bars.append(f'<div class="sw-shimmer" style="height:16px;'
                    f'width:{[100, 92, 96, 88, 94][i % 5]}%"></div>')
    return "".join(bars)


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

# fresh cloud container has an empty db — seed watchlist/rules from committed state.
# Also re-seed when the committed state is newer than this machine's db: that means
# the other copy of the app (phone) changed something since, and saving over it here
# would silently drop it.
if repo_state.WATCHLIST_JSON.exists() and (
        not db.get_watchlist() or repo_state.repo_newer_than_db()):
    try:
        repo_state.import_from_repo()
    except Exception:
        pass

RATING_BADGE = {"OK": "🟢 OK", "Mixed": "🟡 Mixed", "Weak": "🔴 Weak", "Unknown": "⚪ —"}
STATUS_ICON = {"good": "🟢", "ok": "🟡", "weak": "🔴", "info": "ℹ️"}
PERIODS = {"3 months": 0.25, "6 months": 0.5, "1 year": 1.0, "3 years": 3.0, "5 years": 5.0}


def inr(v) -> str:
    return fmt.inr(v)


def _rupees(v) -> str:
    """Rupee formatter for dataframe columns — same Indian grouping as
    everywhere else, so one screen never shows ₹1,32,168 and ₹132,168."""
    return fmt.inr(v)


def _ist_when(ts: str) -> str:
    """A stored UTC timestamp as India time: 'Fri 14 Aug, 3:12 pm'."""
    d = clock.to_ist(ts)
    return f"{clock.short(d.date())}, {clock.clock_time(d)}" if d else str(ts)[:16]


def sync_to_github() -> tuple[bool, str]:
    """Commit the state/*.json (watchlist + rules) and push, so the GitHub Actions
    alert watcher picks them up. Pull --rebase first so the Action's cooldown
    commits (different files) merge cleanly."""
    repo_state.export_config()
    try:
        subprocess.run(["git", "add", "state/watchlist.json", "state/rules.json",
                        "state/holdings.json", "state/suggestions_history.json",
                        "state/finance_plan.json", "state/mf_holdings.json",
                        "state/advice.json", "state/reminders.json",
                        "state/settings.json"],
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


@st.cache_data(ttl=900, show_spinner=False)
def _sim(symbol: str, exchange: str, years: float, amount: float) -> dict | None:
    """3,000 simulated paths — cached, so nudging the amount doesn't re-run the
    whole thing (and the fetch behind it) on every keystroke."""
    return projection.monte_carlo(datasource.get_history(symbol, exchange),
                                  years, amount)


def monte_carlo_block(symbol, exchange, years, amount, period_label):
    """Shared probability-range renderer. Plain-first: the odds verdict leads,
    the quant labels ride along in brackets so users learn the terms."""
    with st.spinner("Running the projection…"):
        mc = _sim(symbol, exchange, years, amount)
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

    # Order is deliberate: the two buttons you reach for most sit at the top,
    # where they're reachable on a phone without scrolling a drawer. Adding a
    # symbol is occasional, so it's collapsed below them.
    if st.button("🔄 Refresh prices now", width="stretch",
                 help="Data is cached ~15 min. This clears it and refetches."):
        with st.spinner("Clearing caches…"):
            datasource._CACHE.clear()
            st.cache_data.clear()
        st.toast("Cleared cache — pulling fresh data")
        st.rerun()

    if st.button("🔔 Run alert check now", width="stretch",
                 help="Checks every active rule right now and notifies if one matches."):
        with st.spinner("Checking your rules against live prices…"):
            fired = watcher.run_once(verbose=False)
        st.toast(f"{len(fired)} alert(s) fired" if fired else "Checked — nothing triggered")

    with st.expander("➕ Add to watchlist"):
        with st.form("add_symbol", clear_on_submit=True):
            new_sym = st.text_input("Symbol", placeholder="TCS, INFY, RELIANCE…").strip().upper()
            new_exch = st.selectbox("Exchange", ["NSE", "BSE"])
            if st.form_submit_button("Add", width="stretch") and new_sym:
                with st.spinner(f"Looking up {new_sym}…"):
                    name = datasource.resolve_name(new_sym, new_exch)
                    db.add_to_watchlist(new_sym, new_exch, name)
                    auto_sync()
                st.toast(f"Added {new_sym}")
                st.rerun()

    st.divider()
    # one line, not one row per channel — this status almost never changes
    _ch = alerts.channel_status()
    _on = [c for c, ok in _ch.items() if ok]
    st.caption(f"🔔 Alerts go to **{' + '.join(_on)}**" if _on else
               "⚪ No notification channel configured yet")

    _ss = _sync_status.get("state")
    if _ss == "syncing":
        st.caption("🔄 Saving to GitHub in the background…")
    elif _ss == "failed":
        st.caption(f"⚠️ Last sync failed: {_sync_status.get('msg', '')[:80]} — "
                   "changes are saved locally; hit retry below.")
    elif _ss == "ok":
        st.caption(f"🟢 Saved to GitHub · {_sync_status.get('msg', '')[:60]}")
    elif gh_sync.available():
        st.caption("🟢 Changes auto-save to GitHub (works from your phone too).")
    else:
        st.caption("🟢 Changes save here and push via git. For edits made on the "
                   "hosted app to stick too, add a STOCKWATCH_GH_TOKEN secret "
                   "(see DEPLOY.md).")
    if st.button("⬆️ Save to GitHub now", width="stretch",
                 help="Changes already save on their own; use this to retry a failed sync."):
        st.session_state["_autosync_dead"] = False
        with st.spinner("Pushing to GitHub…"):
            ok, msg = sync_to_github()
            if not ok and gh_sync.available():
                ok, msg = gh_sync.push_state()
        _sync_status.update(state="ok" if ok else "failed", msg=msg)
        st.toast(("✅ " if ok else "⚠️ ") + msg)


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
            if r.get("monthly"):
                tag = " · every month"
                if r.get("until"):
                    tag += f" till {advice.pretty_date(str(r['until']))}"
                if eff is None:
                    when = "finished"
            elif r.get("yearly"):
                tag = " · every year"
            else:
                tag = ""
            flag = " ⏰" if reminders.due(r, _rtoday) else ""
            done = " · ✅ done" if r.get("done") and not reminders.repeats(r) else ""
            st.markdown(f"- **{when}**{tag} — {r['text']}{flag}{done}")
    else:
        st.caption("No reminders yet. Add one below (e.g. the April PPF top-up).")

    with st.expander("➕ Add / manage reminders"):
        with st.form("add_reminder", clear_on_submit=True):
            rc1, rc2 = st.columns([3, 1.3])
            r_text = rc1.text_input("What to remember", placeholder="Deposit ₹1.5L into PPF")
            r_date = rc2.date_input("Date", format="DD/MM/YYYY")
            rc3, rc4 = st.columns([1.3, 1.3])
            r_repeat = rc3.selectbox("Repeats", ["never", "every month", "every year"])
            r_until = rc4.date_input("Ends (optional, for monthly)", value=None,
                                     format="DD/MM/YYYY")
            if st.form_submit_button("Add reminder") and r_text.strip():
                rlist.append(reminders.new(
                    r_text.strip(), r_date.isoformat(),
                    yearly=(r_repeat == "every year"),
                    monthly=(r_repeat == "every month"),
                    until=r_until.isoformat() if r_until else None))
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
                if not reminders.repeats(r) and q2.button("Done", key=f"rem_done_{i}"):
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
# First load of a session pulls every symbol in parallel and can take a few
# seconds; say so instead of showing an empty page.
_warm_ph = st.empty()
if not st.session_state.get("_warmed"):
    _warm_ph.markdown(f'<div style="padding:4px 0 10px">'
                      f'<div style="font:400 14px/1.6 system-ui;color:#94a3b8">'
                      f'Pulling live prices for your stocks…</div>{skeleton(3)}</div>',
                      unsafe_allow_html=True)
_warm_caches([(w["symbol"], w["exchange"]) for w in watchlist]
             + [(h["symbol"], h["exchange"]) for h in db.get_holdings()]
             + [(r["symbol"], r["exchange"]) for r in db.get_rules(active_only=False)])
st.session_state["_warmed"] = True
_warm_ph.empty()

PREFS = settings.load()


def explain(text: str) -> None:
    """A 'how to read this' caption — hidden when you've turned explainers off
    in Settings, because the same paragraph stops being help after a week."""
    if PREFS.get("explainers", True):
        st.caption(text)


@st.cache_data(ttl=900, show_spinner=False)
def _tips(bucket: str) -> list[dict]:
    """Everything the app has noticed, from data that's already warm.

    Cached for 15 minutes so it costs nothing per rerun — the banner must never
    be the reason a tap feels slow.
    """
    holdings = db.get_holdings()
    prices, ratings = {}, {}
    for h in holdings:
        v = watcher.gather_values(h["symbol"], h["exchange"])
        prices[h["symbol"]] = v.get("price")
    lots = [portfolio.lot_row(h, {"price": prices.get(h["symbol"]),
                                  "pct_change_day": None}) for h in holdings]
    positions = portfolio.by_symbol(lots)
    for p in positions[:6]:            # ratings only for the big ones — it's slow
        try:
            ratings[p["symbol"]] = analysis.score_fundamentals(
                p["symbol"], "NSE").get("rating")
        except Exception:
            pass
    return insights.collect(
        positions=positions[:12],
        tail={"count": len(positions[12:]),
              "value": sum(p["value"] or 0 for p in positions[12:]),
              "pnl": sum(p["pnl"] or 0 for p in positions[12:]),
              "names": ""} if len(positions) > 12 else None,
        totals=portfolio.totals(lots) if lots else {},
        holdings=holdings, prices=prices, ratings=ratings,
        advice_rows=advice.load_advice() or [], rules=db.get_rules(active_only=False),
        history=db.get_alert_history(limit=100), reminders_rows=reminders.load() or [],
        mf_rows=mf.load_mf() or [])


if PREFS.get("banner", True):
    _all_tips = _tips(clock.ist_now().strftime("%Y%m%d%H") +
                      str(clock.ist_now().minute // 15))
    _shown = insights.choose(_all_tips, n=int(PREFS.get("banner_tips", 2)),
                             seed=st.session_state.get("_tip_seed", 0),
                             categories=PREFS.get("banner_categories"),
                             min_urgency=int(PREFS.get("banner_min_urgency", 0)))
    if _shown:
        with st.container(border=True):
            bc1, bc2 = st.columns([12, 1])
            with bc1:
                for t in _shown:
                    icon = "⚠️" if t["urgency"] >= 60 else "💡"
                    st.markdown(f"{icon} **{t['text']}**")
                    line = " ".join(x for x in (t.get("why"), t.get("action")) if x)
                    if line:
                        st.caption(line)
            if bc2.button("↻", key="tip_next", help="Show me different tips"):
                st.session_state["_tip_seed"] = \
                    st.session_state.get("_tip_seed", 0) + 1
                st.rerun()

tabs = st.tabs(["📋 Overview", "💼 Portfolio", "💡 Suggestions",
                "🔍 Stock analysis", "🔔 Alerts", "🗺️ Plan", "🧭 Advice",
                "💬 Advisor", "🎯 IPO", "🛒 Buy", "⚙️ Settings"])

# ================================================================ overview
with tabs[0]:
    st.subheader("Your watchlist")
    if not watchlist:
        st.info("Watchlist is empty — add a symbol from the sidebar (try TCS, INFY, RELIANCE).")
    else:
        rows = []
        _ov_ph = st.empty()
        _ov_ph.markdown(skeleton(len(watchlist)), unsafe_allow_html=True)
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
                  .format({"Price": _rupees, "Day %": "{:+.1f}%", "1Y %": "{:+.1f}%",
                           "P/E": "{:.1f}", "ROE %": "{:.1f}%", "RSI": "{:.0f}"}, na_rep="—"))
        _ov_ph.empty()
        st.dataframe(styled, width="stretch", hide_index=True)
        explain("**Health** = share of fundamental checks passed: 65+ 🟢 OK · 40–64 🟡 Mixed · "
                   "<40 🔴 Weak — about the business, not the price. **RSI** is a 0–100 momentum "
                   "gauge (under 30 = heavily sold off, over 70 = heavily bought). **P/E** = price "
                   "÷ a year's profit per share. Open **Stock analysis** for the deep view + bottom "
                   "line. Prices via NSE live where available, else ~15-min delayed.")

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
        _pf_ph = st.empty()
        _pf_ph.markdown(skeleton(min(len(holdings), 8)), unsafe_allow_html=True)
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
        _pf_ph.empty()
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("You put in", inr(tot["invested"]))
        t2.metric("Worth now", inr(tot["value"]), fmt.pct(tot["pnl_pct"])
                  if tot["pnl_pct"] is not None else None)
        t3.metric("Profit / loss", inr(tot["pnl"]),
                  "in profit" if tot["pnl"] >= 0 else "in loss")
        t4.metric("Today", inr(tot["day_move"]), fmt.pct(tot["day_pct"])
                  if tot["day_pct"] is not None else None)
        if tot["missing"]:
            n = tot["missing"]
            st.warning(f"{n} holding{'' if n == 1 else 's'} had no live price this "
                       f"run, so the totals leave {'it' if n == 1 else 'them'} out. "
                       f"A broker-statement name like “NIPPON ETF JUNI.” never "
                       f"prices — replace it with the NSE symbol below. Otherwise "
                       f"hit 🔄 Refresh prices in the sidebar.")

        pdf = pd.DataFrame(rows)

        def _pl_color(x):
            if isinstance(x, (int, float)):
                return "color: #4ade80" if x > 0 else "color: #fb7185" if x < 0 else ""
            return ""

        st.dataframe(
            pdf.style.map(_pl_color, subset=["Day %", "P&L", "P&L %"])
               .format({"Qty": "{:g}", "Buy ₹": _rupees, "Now ₹": _rupees,
                        "Day %": "{:+.1f}%", "Invested": _rupees, "Value": _rupees,
                        "P&L": lambda v: fmt.signed_inr(v), "P&L %": "{:+.1f}%"},
                       na_rep="—"),
            width="stretch", hide_index=True)
        explain("P&L is vs your buy price (dividends not counted). Health = the business "
                   "quality read — open **Stock analysis** for the full picture + bottom line.")

        with st.expander("⚙️ Manage holdings"):
            for h in holdings:
                c1, c2 = st.columns([4, 1])
                bd = f" · bought {advice.pretty_date(h['buy_date'])}" \
                    if h.get("buy_date") else ""
                c1.write(f"{h['qty']:g} × **{h['symbol']}** @ {inr(h['buy_price'])}{bd}")
                if c2.button("Remove", key=f"rmh_{h['id']}"):
                    db.remove_holding(h["id"])
                    auto_sync()
                    st.rerun()

    # ---------------------------------------------------------- mutual funds
    st.divider()
    st.subheader("🪙 Mutual funds")
    explain("Units × the **official AMFI NAV** (published daily by the MF industry "
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
        _mf_ph = st.empty()
        _mf_ph.markdown(skeleton(min(len(mf_rows), 6)), unsafe_allow_html=True)
        for h in mf_rows:
            # each miss is a 12s AMFI timeout, so this block can be genuinely
            # slow the first time in a session — hence the shimmer above
            nav = _nav(str(h["code"])) if h.get("code") else None
            if h.get("code") and nav is None:
                missing_nav += 1
            vrows.append(mf.value_row(h, nav))
        _mf_ph.empty()
        tot_val = sum(r["value"] for r in vrows if r["value"])
        tot_inv = sum(r["invested"] for r in vrows if r["invested"])
        live_n = sum(1 for r in vrows if r["source"].startswith("live"))
        m1, m2, m3 = st.columns(3)
        m1.metric("Funds worth", inr(tot_val),
                  help="Units × today's official AMFI NAV")
        m2.metric("Priced from AMFI", f"{live_n} of {len(vrows)}",
                  help="Funds where we found today's published NAV")
        m3.metric("Still estimates", f"{len(vrows) - live_n}",
                  help="Rows missing units or cost — fill them in from your CAS "
                       "statement and they turn into real numbers")
        if missing_nav:
            st.warning(f"{missing_nav} fund{'' if missing_nav == 1 else 's'} had no "
                       f"NAV this run — showing estimates for "
                       f"{'it' if missing_nav == 1 else 'them'}.")
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
               .format({"Units": "{:,.3f}", "NAV ₹": "₹{:,.2f}", "Value": _rupees,
                        "Invested": _rupees, "P&L": lambda v: fmt.signed_inr(v),
                        "P&L %": "{:+.1f}%"}, na_rep="—"),
            width="stretch", hide_index=True)
        explain("P&L needs the invested amount — rows without it show value only. "
                   "The monthly CAS statement (CAMS/KFintech email) has exact units for "
                   "every fund you own anywhere; use it to replace the estimates.")

    if os.environ.get("STOCKWATCH_STATE_KEY"):
        with st.expander("➕ Add a fund (AMFI search)"):
            q = st.text_input("Scheme name", placeholder="parag parikh flexi",
                              key="mf_q")

            # cached, or every keystroke fires a fresh 12s-timeout AMFI lookup
            @st.cache_data(ttl=3600, show_spinner="Searching AMFI schemes…")
            def _mf_search(term: str):
                return mf.search_schemes(term)

            hits = _mf_search(q.strip()) if len(q.strip()) >= 4 else []
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
    explain("Ranked by an opportunity score from **real data** — fundamental health, "
               "distance below analysts' target, and trend. Each pick is then deep-checked "
               "(statements, peers, bear case) so you see the reasons *for and against*.")

    c1, c2, c3, c4 = st.columns([1.4, 1, 1, 0.8])
    universe_choice = c1.radio("Scan", ["Popular large-caps", "My watchlist", "Both"])
    period_label = c2.selectbox("Holding period", list(PERIODS.keys()), index=2)
    amount = c3.number_input("Amount (₹)", min_value=1000, value=100000, step=10000)
    # labelled "Deep-check" not "Show": it decides how many get the expensive
    # second pass at scan time, so changing it after a scan does nothing until
    # you scan again
    top_n = c4.slider("Deep-check", 3, 10, 5,
                      help="How many of the top-ranked stocks get the full "
                           "statements/peers/bear-case pass. Takes effect on the "
                           "next scan.")
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
            # a scan is 30-60s of sequential fetches; a bare spinner that long
            # reads as a hang, so say which stage it's in
            with st.status(f"Scanning {len(set(uni))} stocks…", expanded=True) as _sc:
                st.write(f"Ranking all {len(set(uni))} on a quick read…")
                ranked_now = suggestions.rank(uni, top_n=top_n)
                st.write(f"Deep-checking the top {top_n} (statements, peers, "
                         f"valuation)…")
                st.session_state["suggestions"] = ranked_now
                st.session_state["suggestions_ts"] = datetime.now().strftime("%d %b %Y, %H:%M")
                try:                       # persist so the next visit is instant
                    import pickle
                    SUGG_CACHE.write_bytes(pickle.dumps(
                        {"ts": st.session_state["suggestions_ts"], "rows": ranked_now}))
                except Exception:
                    pass
                st.write("Saving this scan to history…")
                _sc.update(label=f"Scanned {len(set(uni))} stocks", state="complete",
                           expanded=False)
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
        # the deep read is five separate pulls (statements, 5y history, peers) and
        # is the slowest thing in the app on a cold cache — show the wait here,
        # shaped like the page that's coming
        _an_ph = st.empty()
        _an_ph.markdown(
            f'<div style="font:400 14px/1.6 system-ui;color:#94a3b8">'
            f'Reading {picked_sym}: statements, 5-year history, peers…</div>'
            f'{skeleton(7)}', unsafe_allow_html=True)
        score = analysis.score_fundamentals(picked_sym, picked_exch, deep=True)
        vals = watcher.gather_values(picked_sym, picked_exch)
        hist = datasource.get_history(picked_sym, picked_exch)
        val = bearcase.valuation_percentile(picked_sym, picked_exch)
        peer = sectors.peer_comparison(picked_sym, picked_exch)
        _an_ph.empty()

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
        with st.spinner("Checking what could go wrong…"):
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
        with st.spinner(f"Replaying “{sig}” across this stock's history…"):
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
        cond_txt = " and ".join(watcher.plain_condition(c) for c in rule["conditions"])
        active = bool(rule["active"])
        mode_tag = (" · ⚡ pings once when it crosses"
                    if rule.get("mode") == "edge"
                    else " · 🔁 stays on while it's true (at most one mail a day)")
        if rule.get("true_since"):
            mode_tag += f" · true since {advice.pretty_date(rule['true_since'])}"
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
        # stored in UTC; shown in IST, because "09:42" on a 3:12pm alert is a lie
        st.dataframe(pd.DataFrame([{"When": _ist_when(h["ts"]), "Symbol": h["symbol"],
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
            with st.spinner("Rewriting the exit alerts…"):
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

# ================================================================ ipo screener
with tabs[8]:
    st.subheader("🎯 IPO screener — apply-for-the-listing-pop rules")
    explain("House rules, tuned for the flip strategy (apply → sell in the first "
               "30 min of listing): **Mainboard** needs GMP ≥ 20% + total ≥ 15x + "
               "QIB ≥ 5x · **SME** needs GMP ≥ 35% + total ≥ 25x + QIB ≥ 2x (min "
               "application ~₹2L+, pure lottery). Always apply on the LAST day, "
               "late morning, 1 lot per PAN. GMP is unofficial and easiest to fake "
               "in SME issues — QIB numbers are the honesty check. Data: "
               "investorgain.com live (updates through the day; ipowatch.in as "
               "fallback), also in the 12:05pm Telegram brief.")

    @st.cache_data(ttl=600, show_spinner=False)
    def _ipo_screen_cached(bucket: str):
        return ipo.screen()

    if st.button("🔎 Check open IPOs now", type="primary"):
        st.session_state["ipo_checked"] = True
    if st.session_state.get("ipo_checked"):
        with st.spinner("Pulling live GMP + subscription numbers…"):
            # 10-min cache bucket so a tab hop doesn't re-scrape but a manual
            # recheck during the last-day window stays close to live
            ipo_rows = _ipo_screen_cached(datetime.now().strftime("%Y%m%d%H")
                                          + str(datetime.now().minute // 10))
        if not ipo_rows:
            st.warning("Couldn't read any IPO data right now — the source page "
                       "may be down or reshaped. Try again in a bit, or check "
                       "investorgain.com directly.")
        else:
            upd = next((r["updated"] for r in ipo_rows if r.get("updated")), "")
            st.caption(f"Source: {ipo_rows[0].get('source', '?')}"
                       + (f" · subscription updated **{upd}**" if upd else "")
                       + " · this page re-pulls at most every 10 min")
            badge = {"APPLY-ZONE": "🟢", "WATCH": "🟡", "SKIP": "🔴", "NO DATA": "⚪"}
            today_ist = clock.ist_today()
            for r in ipo_rows:
                pct = f"{r['gmp_pct']:g}%" if r.get("gmp_pct") is not None else "?"
                tot = f"{r['total']:g}x" if r.get("total") is not None else "?"
                qib = f"{r['qib']:g}x" if r.get("qib") is not None else "?"
                last_day = r.get("closes") == today_ist
                st.markdown(
                    f"{badge.get(r['verdict'], '⚪')} **{r['name']}** "
                    f"({'SME' if r.get('sme') else 'Mainboard'}) — GMP {pct} · "
                    f"total {tot} · QIB {qib} · "
                    + ("**⏳ last day — bids close 4 pm today**" if last_day
                       else ipo.closing_phrase(r, today_ist)))
                st.caption(f"{r['verdict']}: {r['why']}")
            explain("APPLY-ZONE = passes every bar **today** — still apply only "
                       "on the last day. WATCH = GMP qualifies but the book is "
                       "still filling (normal on day 1-2). Numbers move all day; "
                       "recheck before paying.")

# ================================================================ buy advisor
with tabs[9]:
    st.subheader("🛒 Buy advisor — one score, one pick")
    explain("Searches **Amazon + Flipkart + Myntra** live, drops listings "
               "that don't match what you typed, and boils each one down to "
               "a **0–100 score**: 45% quality (rating, shrunk so a handful "
               "of 5★ reviews can't beat thousands of 4★ ones) · 30% match "
               "with your words · 15% price inside your budget · 10% review "
               "depth · minus a nick for fake-MRP anchor tricks. "
               "Top of the list = buy that one. 📌 tracks an item: the "
               "midday brief re-checks its price every weekday and pings "
               "you on a ≥3% drop.")

    with st.form("shop_form"):
        c1, c2 = st.columns([3, 1])
        shop_q = c1.text_input("What are you buying?",
                               placeholder="e.g. white running shoes men")
        shop_cap = c2.number_input("Budget ₹ (0 = any)", min_value=0,
                                   value=1000, step=100)
        shop_go = st.form_submit_button("🔎 Search & judge", type="primary")

    if shop_go and shop_q.strip():
        st.session_state["shop_q"] = shop_q.strip()
        st.session_state["shop_cap"] = shop_cap or None

    def _track_btn(r, key):
        if st.button("📌 Track", key=key,
                     help="Daily price check + Telegram ping on a ≥3% drop"):
            if shop_watch.add(r["title"], r["url"], r["source"], r.get("price")):
                auto_sync()
                st.toast(f"Tracking {r['title'][:40]}…")
            else:
                st.error("Couldn't save — state key missing.")

    if st.session_state.get("shop_q"):
        q, cap = st.session_state["shop_q"], st.session_state.get("shop_cap")

        @st.cache_data(ttl=1800, show_spinner=False)
        def _shop_cached(query: str, max_price, bucket: str):
            return shop.advise(query, max_price)

        # this one really does take up to ~90s (three stores, retries, polite
        # sleeps), so it gets a staged status rather than a spinner that looks
        # like the app died
        with st.status(f"Searching for “{q}”…", expanded=True) as _sh:
            st.write("Asking Amazon, Flipkart and Myntra… (up to ~1 min, they "
                     "throttle rapid requests)")
            found = _shop_cached(q, cap, datetime.now().strftime("%Y%m%d%H")
                                 + str(datetime.now().minute // 30))
            st.write(f"Scoring {len(found)} listings that matched your words…")
            _sh.update(label=f"Found {len(found)} listings for “{q}”",
                       state="complete", expanded=False)
        srcs = {r["source"] for r in found}
        missing = [s for s in ("Amazon", "Flipkart", "Myntra")
                   if s not in srcs]
        if not found:
            st.warning("No store answered (cloud servers get blocked; even "
                       "from home they throttle rapid retries). Hand-search "
                       "links below — or ask Claude to pull them.")
        elif missing:
            st.caption(f"⚠️ {' + '.join(missing)} didn't answer this time "
                       "(block/throttle) — ranking uses the stores that did. "
                       "Hand-search links at the bottom.")

        badge = {"PICK-ZONE": "🟢", "OK": "🟡", "UNRATED": "⚪",
                 "RISKY": "🟠", "AVOID": "🔴"}
        if found:
            top = found[0]
            stars = f"{top['rating']:g}★" if top.get("rating") else "?★"
            rev = f"{int(top['reviews']):,} reviews" if top.get("reviews") \
                else "reviews unknown"
            st.success(f"🏆 **Best bet — score {top['score']}/100** · "
                       f"₹{top['price']:g} · {stars} · {rev} · {top['source']}\n\n"
                       f"**[{top['title'][:90]}]({top['url']})**\n\n{top['why']}")
            tc1, tc2 = st.columns([1, 6])
            with tc1:
                _track_btn(top, "trk_top")
            if top.get("history"):
                with st.expander("📉 price history (1 year, live from Keepa)",
                                 expanded=True):
                    st.image(top["history"])

        for i, r in enumerate(found[1:8]):
            left, right = st.columns([11, 2])
            stars = f"{r['rating']:g}★" if r.get("rating") else "?★"
            rev = f"{int(r['reviews']):,}" if r.get("reviews") else "?"
            left.markdown(
                f"{badge.get(r['verdict'], '⚪')} **₹{r['price']:g}** · "
                f"{stars} ({rev}) · {r['source']} — "
                f"[{r['title'][:65]}]({r['url']})")
            left.caption(f"{r['verdict']}: {r['why']}")
            with left:
                bc1, bc2 = st.columns([1, 5])
                with bc1:
                    _track_btn(r, f"trk{i}")
                if r.get("history"):
                    with bc2:
                        with st.expander("📉 price history"):
                            st.image(r["history"])
            right.markdown(f"<div style='text-align:center;font-size:1.5em;"
                           f"font-weight:700'>{r['score']}</div>"
                           "<div style='text-align:center;font-size:0.7em;"
                           "opacity:0.6'>/100</div>", unsafe_allow_html=True)
        if len(found) > 8:
            with st.expander(f"{len(found) - 8} more (lower scores)"):
                for r in found[8:30]:
                    stars = f"{r['rating']:g}★" if r.get("rating") else "?★"
                    st.markdown(f"`{r['score']:>3}` ₹{r['price']:g} · {stars} "
                                f"· {r['source']} — [{r['title'][:60]}]"
                                f"({r['url']})")
        if found:
            st.caption("📉 charts: Amazon items get Keepa's full history "
                       "instantly; Flipkart/Myntra have no free source — "
                       "📌 Track them and the app builds its own, one point "
                       "per weekday. Prices move all day during sales — "
                       "re-search before paying.")
        links = shop.search_urls(q)
        st.markdown("Hand-search: " + " · ".join(
            f"[{name}]({url})" for name, url in links.items()))

    # ----------------------------------------------------------- tracked
    tracked = shop_watch.load() or []
    if tracked:
        st.divider()
        st.markdown(f"#### 📌 Tracked items ({len(tracked)})")
        st.caption("Re-priced every weekday by the midday brief; Telegram "
                   "ping on a ≥3% drop or your target price.")
        for i, item in enumerate(tracked):
            hist = item.get("history", [])
            last = hist[-1]["p"] if hist else None
            low = min((pt["p"] for pt in hist), default=None)
            c1, c2 = st.columns([8, 4])
            with c1:
                st.markdown(f"**[{item['title'][:70]}]({item['url']})** · "
                            f"{item['source']}")
                bits = []
                if last is not None:
                    bits.append(f"last ₹{last:g}")
                if low is not None and low != last:
                    bits.append(f"low ₹{low:g}")
                bits.append(f"since {item.get('added', '?')}")
                st.caption(" · ".join(bits))
                a = shop.asin(item["url"]) if item["source"] == "Amazon" else None
                if a:
                    with st.expander("📉 full history (Keepa)"):
                        st.image(shop.keepa_png(a))
                if st.button("🗑 Untrack", key=f"untrk{i}"):
                    shop_watch.remove(item["url"])
                    auto_sync()
                    st.rerun()
            with c2:
                if len(hist) >= 2:
                    st.line_chart(
                        pd.DataFrame({"₹": [pt["p"] for pt in hist]},
                                     index=[pt["d"][5:] for pt in hist]),
                        height=140)
                else:
                    st.caption("chart appears after a couple of daily checks")

# ================================================================== settings
with tabs[10]:
    st.subheader("⚙️ Settings")
    st.caption("Preferences live in `state/settings.json` and sync with the rest "
               "of the app, so the phone and the laptop agree.")

    with st.form("prefs"):
        st.markdown("**💡 Smart banner**")
        st.caption("The strip above the tabs. Every line in it is computed from "
                   "your own holdings, rules, ledger and reminders — it's the app "
                   "noticing things across tabs that no single tab can see.")
        p_banner = st.checkbox("Show the banner", value=PREFS["banner"])
        pb1, pb2 = st.columns(2)
        p_count = pb1.slider("How many at once", 1, 3,
                             int(PREFS["banner_tips"]))
        p_urgency = pb2.select_slider(
            "What's worth showing", options=[0, 40, 60],
            value=int(PREFS.get("banner_min_urgency", 0)),
            format_func=lambda v: {0: "Everything, including house tips",
                                   40: "Skip the general tips",
                                   60: "Only things that look urgent"}[v])
        p_cats = st.multiselect(
            "Kinds of tip", insights.CATEGORIES,
            default=PREFS.get("banner_categories", insights.CATEGORIES),
            format_func=lambda c: {
                "risk": "risk — concentration, unprotected losses",
                "money": "money — where gains and losses actually sit",
                "tax": "tax — long-term dates, advance-tax instalments",
                "hygiene": "hygiene — missing data, rules gone quiet",
                "ipo": "ipo — how the flip rules work",
                "habit": "habit — overdue reminders and reviews"}[c])

        st.markdown("**📬 Mails**")
        p_digest = st.checkbox(
            "Put the top tip in the daily digest", value=PREFS["digest_tips"],
            help="Adds one 'worth knowing' line to the after-close mail.")

        st.markdown("**📖 Reading help**")
        p_explain = st.checkbox(
            "Show the 'how to read this' captions", value=PREFS["explainers"],
            help="The long grey paragraphs under each table. Useful at first, "
                 "clutter once you know the app.")

        if st.form_submit_button("Save settings", type="primary"):
            settings.save({"banner": p_banner, "banner_tips": p_count,
                           "banner_categories": p_cats, "digest_tips": p_digest,
                           "banner_min_urgency": p_urgency,
                           "explainers": p_explain})
            auto_sync()
            st.toast("Settings saved")
            st.rerun()

    with st.expander("🔍 Everything the app has noticed right now"):
        st.caption("The full list the banner picks from, most urgent first. If "
                   "something here looks wrong, it's a bug worth telling me about "
                   "— each line is computed, not generated.")
        for t in _tips(clock.ist_now().strftime("%Y%m%d%H") +
                       str(clock.ist_now().minute // 15)):
            st.markdown(f"**{t['text']}** &nbsp;`{t['category']}` "
                        f"<span style='opacity:.55'>urgency {t['urgency']}</span>",
                        unsafe_allow_html=True)
            if t.get("why") or t.get("action"):
                st.caption(" ".join(x for x in (t.get("why"), t.get("action")) if x))
