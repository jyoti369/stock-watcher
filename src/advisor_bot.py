"""In-app financial advisor chat.

Answers questions like "what should I do with PVRINOX?" by mixing three things:

  1. YOUR data — the whole app, decrypted: equity holdings priced live (with
     P&L), mutual funds at AMFI NAV, the money plan and its open checklist,
     the buy/sell advice ledger, watchlist, armed alerts and reminders. All
     read locally; only the packed summary goes out with the question.
  2. LIVE market data — current price, day move, fundamentals health and the
     analyst target for any stock the question is about.
  3. FRESH web headlines — reused from ai_insights.gather_news (free Google
     News + Yahoo), so the answer reflects today, not the model's stale memory.

Those are packed into a prompt with a fixed advisor brief (your profile, risk
level, tax slab, and the standing plan) and sent to Gemini/OpenAI. Unlike the
read-only insight layer, this one MAY give a clear stance — that's the point —
but the brief forces honest ranges over promises, plain language over jargon,
and "screenshot before you submit / this is not execution" discipline.

It can also PROPOSE changes (a reminder, a ledger call, a price alert, ticking
off a plan item). Proposals are structured JSON, never silent writes: the app
renders them as buttons and `apply_action` does the write in plain Python only
after you tap confirm. The model never touches your files.

Requires the state key (to read your encrypted data) and an AI key.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from . import (advice, ai_insights, analysis, db, finance_plan, mf, portfolio,
               reminders, watcher)
from .repo_state import _read_maybe_enc, WATCHLIST_JSON

BRIEF = (
    "You are this user's personal financial advisor inside their private app. "
    "You know them well; speak directly and warmly, like an ongoing advisor, not "
    "a generic bot.\n\n"
    "WHO THEY ARE: 24, software engineer in rural West Bengal, ~Rs 2.5L/month "
    "income, near-zero expenses, NEW tax regime (30% slab), risk appetite LOW. "
    "They earn well but are still learning money vocabulary — so ALWAYS explain "
    "any jargon in one plain phrase the first time it appears.\n\n"
    "THE PLAN ALREADY IN MOTION (don't contradict it without flagging): monthly "
    "SIPs (ICICI Nifty 50 Index 40k + Parag Parikh Flexi 30k + gold 8k), PPF "
    "1.5L paid yearly as an April lump sum, an 8L arbitrage-fund parking that "
    "drips into the index via a 10-month STP, a cleaned-up mutual-fund book, FDs "
    "being gifted to parents at maturity, and a slow stock-portfolio cleanup.\n\n"
    "HOW TO ANSWER:\n"
    "- You have their FULL app data below: every holding with share count and "
    "live P&L, funds, plan, checklist, ledger, alerts and reminders. Use the "
    "real numbers. NEVER say you cannot see their holdings or tell them to go "
    "check their broker screen — if a figure is genuinely absent from the data "
    "below, say which one is missing.\n"
    "- Lead with a clear stance when asked (keep / trim / sell / wait) — that is "
    "why you exist. But show the reasoning and the risks, never a bare verdict.\n"
    "- Ground every stock claim in the LIVE FIGURES and HEADLINES provided. If the "
    "news is thin or the data is missing, say so — never invent numbers.\n"
    "- Honest projections only: use ranges, never guarantees or precise targets; "
    "say plainly when something is uncertain.\n"
    "- Frame returns after tax when it matters at their 30% slab.\n"
    "- Prefer their existing plan and low-risk lean; discourage churn, hype, and "
    "tips. Contribution matters more than chasing risk.\n"
    "- You ADVISE; you do not execute trades. For any trade, remind them it's "
    "their call and to check the order screen before submitting.\n"
    "- Keep it tight and readable: a few short paragraphs, plain American English, "
    "no markdown headers, no em-dashes, no emoji."
)

ACTION_BRIEF = (
    "\n\nSETTING THINGS UP FOR THEM: you can set a reminder, add a call to the "
    "buy/sell ledger, arm a price alert, or tick off a plan checklist item. When "
    "the user asks for one (or when your own answer promises to track something), "
    "append ONE fenced block at the very end, after your prose:\n"
    "```actions\n"
    '[{"type": "add_reminder", "text": "Review ITC position", '
    '"date": "2026-10-01", "yearly": false, "why": "the October review you asked for"}]\n'
    "```\n"
    "Allowed types and fields:\n"
    '- add_reminder: text, date (YYYY-MM-DD), yearly (true/false)\n'
    '- add_advice: symbol, stance (KEEP/TRIM/SELL/HOLD-RULE/WATCH), thesis, '
    'catalyst, catalyst_date (YYYY-MM-DD), sell_above (number), stop_below '
    '(number), review_by (YYYY-MM-DD)\n'
    '- add_alert: symbol, metric (price/pct_change_day/rsi14), op (< or >), '
    'value (number), label\n'
    '- check_plan_item: text (must closely match an OPEN checklist line above)\n'
    "Every action needs a short 'why'. Only real dates, never blanks. Nothing is "
    "saved until the user taps confirm, so say in your prose what you have queued "
    "up for them. No actions needed? Omit the block entirely."
)

_NAME_HINTS = {
    "pvr": "PVRINOX", "asian": "ASIANPAINT", "paint": "ASIANPAINT",
    "infy": "INFY", "infosys": "INFY", "cdsl": "CDSL", "idea": "IDEA",
    "vodafone": "IDEA", "cochin": "COCHINSHIP", "graphite": "GRAPHITE",
    "itc": "ITC", "icici": "ICICIBANK", "angel": "ANGELONE",
    "gold": "GOLDBEES", "suzlon": "SUZLON", "bandhan": "BANDHANBNK",
}

ACTION_TYPES = {"add_reminder", "add_advice", "add_alert", "check_plan_item"}


def resolve_symbols(query: str, universe: list[str]) -> list[str]:
    """Best-effort: which tickers is the question about? Direct ticker hits in
    the user's own universe first, then friendly-name hints."""
    q = query.upper()
    hits = [s for s in universe if re.search(rf"\b{re.escape(s)}\b", q)]
    ql = query.lower()
    for hint, sym in _NAME_HINTS.items():
        if hint in ql and sym not in hits:
            hits.append(sym)
    return hits[:3]


def _holdings_universe() -> list[str]:
    syms = {h["symbol"] for h in db.get_holdings()}
    for a in (advice.load_advice() or []):
        syms.add(a["symbol"])
    for w in _read_maybe_enc(WATCHLIST_JSON, []):
        syms.add(w.get("symbol", ""))
    return sorted(s for s in syms if s)


# ---- context blocks -------------------------------------------------------

def _stock_holdings() -> str:
    """Every equity lot priced LIVE, aggregated per symbol, plus book totals.

    Prices come from the same cached fetch the Portfolio tab uses, pulled in
    parallel, so the advisor quotes the identical P&L you see on screen.
    """
    rows = db.get_holdings()
    if not rows:
        return ""
    pairs = list(dict.fromkeys((h["symbol"], h["exchange"]) for h in rows))
    vals: dict[tuple, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
            for p, v in zip(pairs, ex.map(
                    lambda p: watcher.gather_values(p[0], p[1]), pairs)):
                vals[p] = v or {}
    except Exception:
        vals = {p: {} for p in pairs}

    lots = [portfolio.lot_row(h, vals.get((h["symbol"], h["exchange"]), {}))
            for h in rows]
    agg: dict[str, dict] = {}
    for lot, h in zip(lots, rows):
        a = agg.setdefault(lot["symbol"], {
            "qty": 0.0, "invested": 0.0, "value": 0.0, "price": lot.get("price"),
            "day": lot.get("day_pct"), "dates": []})
        a["qty"] += lot["qty"]
        a["invested"] += lot["invested"] or 0
        a["value"] += lot["value"] or 0
        if h.get("buy_date"):
            a["dates"].append(h["buy_date"])

    lines = []
    for sym, a in sorted(agg.items(), key=lambda kv: -(kv[1]["value"] or 0)):
        avg = a["invested"] / a["qty"] if a["qty"] else 0
        pnl = (a["value"] - a["invested"]) if a["value"] else None
        pct = (pnl / a["invested"] * 100) if (pnl is not None and a["invested"]) else None
        now = f"Rs {a['price']:,.2f}" if a.get("price") else "no live price"
        day = f", day {a['day']:+.1f}%" if isinstance(a.get("day"), (int, float)) else ""
        pl = (f", now Rs {a['value']:,.0f}, P&L Rs {pnl:+,.0f} ({pct:+.1f}%)"
              if pnl is not None else ", current value unavailable")
        since = f", held since {advice.pretty_date(min(a['dates']))}" if a["dates"] else ""
        lines.append(f"  - {sym}: {a['qty']:g} sh, avg buy Rs {avg:,.2f}, "
                     f"invested Rs {a['invested']:,.0f}, price {now}{day}{pl}{since}")

    t = portfolio.totals(lots)
    head = (f"STOCK PORTFOLIO — invested Rs {t['invested']:,.0f}, "
            f"now Rs {t['value']:,.0f}, total P&L Rs {t['pnl']:+,.0f}")
    if t.get("pnl_pct") is not None:
        head += f" ({t['pnl_pct']:+.1f}%)"
    if t.get("missing"):
        head += f" [{t['missing']} lot(s) had no live price]"
    return head + ":\n" + "\n".join(lines)


def _mf_block() -> str:
    rows = mf.load_mf() or []
    if not rows:
        return ""
    lines, total = [], 0.0
    for h in rows:
        nav = mf.latest_nav(str(h["code"])) if h.get("code") else None
        v = mf.value_row(h, nav)
        total += v["value"] or 0
        val = f"Rs {v['value']:,.0f}" if v["value"] else "value unknown"
        pl = (f", P&L Rs {v['pnl']:+,.0f} ({v['pnl_pct']:+.1f}%)"
              if v.get("pnl") is not None and v.get("pnl_pct") is not None else "")
        lines.append(f"  - {h['name']}: {val}{pl} ({v['source']})")
    return f"MUTUAL FUNDS — total about Rs {total:,.0f}:\n" + "\n".join(lines)


def _advice_block() -> str:
    rows = advice.load_advice() or []
    if not rows:
        return ""
    parts = []
    open_calls = [a for a in rows if a.get("status", "OPEN") == "OPEN"]
    if open_calls:
        lines = []
        for a in open_calls:
            bands = []
            if advice._num(a.get("sell_above")):
                bands.append(f"sell above {advice._num(a['sell_above']):g}")
            if advice._num(a.get("stop_below")):
                bands.append(f"stop below {advice._num(a['stop_below']):g}")
            b = f" [{', '.join(bands)}]" if bands else ""
            cat = f" Watch for: {a['catalyst']}" if a.get("catalyst") else ""
            rev = (f" Review by {advice.pretty_date(a['review_by'])}."
                   if a.get("review_by") else "")
            lines.append(f"  - {a['symbol']} {a.get('stance')}: "
                         f"{a.get('thesis')}{b}.{cat}{rev}")
        parts.append("ADVICE LEDGER, open calls (your own standing decisions):\n"
                     + "\n".join(lines))
    closed = [a for a in rows if a.get("status", "OPEN") != "OPEN"]
    if closed:
        parts.append("CLOSED CALLS (scoreboard): " + "; ".join(
            f"{a['symbol']} {a['status']}" for a in closed[:12]))
    return "\n\n".join(parts)


def _alerts_block() -> str:
    rules = [r for r in db.get_rules(active_only=False) if r.get("active")]
    if not rules:
        return ""
    lines = []
    for r in rules[:25]:
        cond = " and ".join(f"{c['metric']} {c['op']} {c['value']:g}"
                            for c in r["conditions"])
        lines.append(f"  - {r['symbol']}: {r.get('label') or 'alert'} ({cond})")
    return "ARMED PRICE ALERTS (fire to Telegram/email 24/7):\n" + "\n".join(lines)


def _reminders_block(today: date) -> str:
    rows = reminders.load() or []
    if not rows:
        return ""
    lines = []
    for r in sorted(rows, key=lambda r: (reminders.effective_date(r, today) or date.max)):
        eff = reminders.effective_date(r, today)
        when = advice.pretty_date(eff.isoformat()) if eff else "?"
        tag = " (every year)" if r.get("yearly") else ""
        flag = " <- DUE NOW" if reminders.due(r, today) else ""
        lines.append(f"  - {when}{tag}: {r['text']}{flag}")
    return "REMINDERS SET:\n" + "\n".join(lines)


def _plan_block() -> str:
    plan = finance_plan.load_plan()
    if not plan or not plan.get("content"):
        return ""
    parts = ["MONEY PLAN (pocket copy):\n" + plan["content"][:2500]]
    try:
        items = finance_plan.checklist_items(plan["content"])
    except AttributeError:
        items = []
    todo = [i["text"] for i in items if not i["done"]]
    if todo:
        parts.append("OPEN CHECKLIST ITEMS (not done yet):\n"
                     + "\n".join(f"  - {t}" for t in todo))
    return "\n\n".join(parts)


def _watchlist_block() -> str:
    wl = [w.get("symbol") for w in db.get_watchlist() if w.get("symbol")]
    owned = {h["symbol"] for h in db.get_holdings()}
    extra = [s for s in wl if s not in owned]
    return ("WATCHING (not owned): " + ", ".join(extra)) if extra else ""


def _money_context(today: date | None = None) -> str:
    """Compact text pack of the whole app: holdings, funds, plan, ledger,
    alerts, reminders and watchlist, stamped with today's date."""
    today = today or date.today()
    blocks = [
        f"TODAY IS {advice.pretty_date(today.isoformat())} "
        f"({today.strftime('%A')}, {today.isoformat()}).",
        _stock_holdings(), _mf_block(), _advice_block(), _reminders_block(today),
        _alerts_block(), _watchlist_block(), _plan_block(),
    ]
    filled = [b for b in blocks if b]
    return "\n\n".join(filled) if len(filled) > 1 else "(no stored portfolio data found)"


def _stock_context(sym: str) -> tuple[str, list[dict]]:
    """Live figures + fresh headlines for one ticker."""
    facts = []
    try:
        v = watcher.gather_values(sym, "NSE")
        if v.get("price") is not None:
            facts.append(f"price Rs {v['price']:,.2f}")
        if isinstance(v.get("pct_change_day"), (int, float)):
            facts.append(f"day {v['pct_change_day']:+.1f}%")
        for k, lbl in (("ret_1m", "1m"), ("ret_1y", "1y")):
            if isinstance(v.get(k), (int, float)):
                facts.append(f"{lbl} {v[k]:+.1f}%")
    except Exception:
        pass
    try:
        s = analysis.score_fundamentals(sym, "NSE")
        if s.get("rating"):
            facts.append(f"health {s['rating']} ({s.get('score')})")
        av = (s.get("analyst") or {}) if isinstance(s, dict) else {}
        if av.get("target"):
            facts.append(f"analyst target Rs {av['target']:,.0f}")
    except Exception:
        pass
    news = ai_insights.gather_news(sym)
    return f"{sym}: " + (", ".join(facts) if facts else "no live figures"), news


# ---- proposed actions -----------------------------------------------------

_ACTION_BLOCK = re.compile(r"```(?:actions|json)\s*(\[.*?\])\s*```", re.S | re.I)


def split_actions(text: str) -> tuple[str, list[dict]]:
    """Pull the trailing ```actions block out of a reply.

    Returns (prose without the block, validated actions). Anything malformed or
    of an unknown type is dropped silently — a broken block must never surface
    as a button that writes something unexpected.
    """
    m = _ACTION_BLOCK.search(text or "")
    if not m:
        return (text or "").strip(), []
    try:
        raw = json.loads(m.group(1))
    except (ValueError, TypeError):
        raw = []
    actions = []
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict) and a.get("type") in ACTION_TYPES:
            actions.append(a)
    prose = (text[:m.start()] + text[m.end():]).strip()
    return prose, actions


def describe_action(a: dict) -> str:
    """One human line for the confirm button."""
    t = a.get("type")
    if t == "add_reminder":
        when = advice.pretty_date(a.get("date"))
        every = " every year" if a.get("yearly") else ""
        return f"Set reminder{every} for {when}: {a.get('text', '')}"
    if t == "add_advice":
        bits = [f"{a.get('symbol', '')} — {a.get('stance', '')}"]
        if a.get("sell_above"):
            bits.append(f"sell above ₹{float(a['sell_above']):g}")
        if a.get("stop_below"):
            bits.append(f"stop below ₹{float(a['stop_below']):g}")
        return "Add ledger call: " + " · ".join(bits)
    if t == "add_alert":
        return (f"Arm alert: {a.get('symbol', '')} {a.get('metric', 'price')} "
                f"{a.get('op', '<')} {a.get('value', '')}")
    if t == "check_plan_item":
        return f"Tick off plan item: {a.get('text', '')}"
    return str(t)


def apply_action(a: dict) -> tuple[bool, str]:
    """Execute one confirmed action in plain Python. (ok, message).

    The model never writes anything; this is the only path, and it validates
    every field itself rather than trusting the proposal.
    """
    t = a.get("type")
    try:
        if t == "add_reminder":
            text = str(a.get("text", "")).strip()
            when = str(a.get("date", ""))[:10]
            if not text:
                return False, "reminder needs text"
            try:
                date.fromisoformat(when)
            except ValueError:
                return False, f"'{when}' is not a valid date"
            rows = reminders.load() or []
            rows.append(reminders.new(text, when, bool(a.get("yearly"))))
            return (True, "reminder saved") if reminders.save(rows) else \
                (False, "no encryption key — nothing written")

        if t == "add_advice":
            sym = str(a.get("symbol", "")).upper().strip()
            stance = str(a.get("stance", "WATCH")).upper().strip()
            if not sym:
                return False, "ledger call needs a symbol"
            if stance not in advice.STANCES:
                stance = "WATCH"
            rows = advice.load_advice() or []
            rows.append(advice.new_entry(
                sym, stance, str(a.get("thesis", "")).strip(),
                str(a.get("catalyst", "")).strip(),
                str(a.get("catalyst_date", ""))[:10],
                advice._num(a.get("sell_above")), advice._num(a.get("stop_below")),
                str(a.get("review_by", ""))[:10]))
            return (True, f"{sym} added to the ledger") if advice.save_advice(rows) else \
                (False, "no encryption key — nothing written")

        if t == "add_alert":
            sym = str(a.get("symbol", "")).upper().strip()
            metric = str(a.get("metric", "price")).strip()
            op = str(a.get("op", "<")).strip()
            if not sym:
                return False, "alert needs a symbol"
            if metric not in watcher.METRICS or op not in watcher.OPS:
                return False, f"can't alert on '{metric} {op}'"
            try:
                value = float(a.get("value"))
            except (TypeError, ValueError):
                return False, "alert needs a number to compare against"
            label = str(a.get("label", "")).strip() or f"{metric} {op} {value:g}"
            db.add_rule(sym, "NSE", label, [{"metric": metric, "op": op,
                                             "value": value}], mode="edge")
            return True, f"alert armed for {sym}"

        if t == "check_plan_item":
            plan = finance_plan.load_plan()
            if not plan:
                return False, "no plan saved"
            want = str(a.get("text", "")).strip().lower()
            items = finance_plan.checklist_items(plan["content"])
            match = next((i for i in items if not i["done"]
                          and (want in i["text"].lower() or i["text"].lower() in want)), None)
            if not match:
                return False, "couldn't find that open checklist item"
            updated = finance_plan.set_check(plan["content"], match["line"], True)
            return (True, f"ticked off: {match['text'][:40]}") \
                if finance_plan.save_plan(updated) else \
                (False, "no encryption key — nothing written")
    except Exception as e:                       # never let a bad proposal crash the chat
        return False, str(e)[:120]
    return False, f"unknown action '{t}'"


# ---- the call -------------------------------------------------------------

def answer(query: str, history: list[dict] | None = None) -> dict[str, Any]:
    """Return {text, actions, sources, engine} or {error}."""
    avail = ai_insights.available()
    if not (avail["gemini"] or avail["openai"]):
        return {"error": "No AI key configured (STOCKWATCH_GEMINI_KEY)."}
    if finance_plan.load_plan() is None and not (mf.load_mf() or advice.load_advice()
                                                 or db.get_holdings()):
        return {"error": "No decrypted data — set STOCKWATCH_STATE_KEY to let the "
                         "advisor read your portfolio."}

    syms = resolve_symbols(query, _holdings_universe())
    stock_blocks, sources = [], []
    for s in syms:
        block, news = _stock_context(s)
        stock_blocks.append(block)
        sources.extend(news)
    if stock_blocks:
        head_lines = "\n".join(f"- {n['title']} ({n['date']})" for n in sources[:8])
        live = ("\n\nLIVE FIGURES for the stock(s) asked about:\n"
                + "\n".join(stock_blocks)
                + (f"\n\nFRESH HEADLINES:\n{head_lines}" if head_lines else ""))
    else:
        live = ""

    convo = ""
    for turn in (history or [])[-4:]:
        who = "User" if turn["role"] == "user" else "You"
        convo += f"\n{who}: {turn['content']}"

    prompt = (f"{BRIEF}{ACTION_BRIEF}\n\nYOUR DATA\n{_money_context()}{live}\n\n"
              f"CONVERSATION SO FAR:{convo}\n\nUser: {query}\n\n"
              "Answer as their advisor now.")
    # Try Gemini (free) first, fall back to OpenAI if it's down rather than
    # handing back a raw HTTP error for an outage that isn't the user's problem.
    order = [e for e in ("gemini", "openai") if avail[e]]
    text, err = None, ""
    for engine in order:
        try:
            text = (ai_insights._gemini(prompt) if engine == "gemini"
                    else ai_insights._openai(prompt))
            break
        except Exception as e:
            err = str(e)[:200]
    if text is None:
        friendly = ("The AI service is busy right now (it retried a few times). "
                    "Give it a minute and ask again.") if "503" in err or "429" in err else err
        return {"error": friendly}
    prose, actions = split_actions(text)
    seen, uniq = set(), []
    for n in sources:
        if n["title"] not in seen:
            seen.add(n["title"])
            uniq.append(n)
    return {"text": prose, "actions": actions, "sources": uniq[:6],
            "engine": ("Gemini" if engine == "gemini" else "OpenAI")
                      + " · your data + live market + news"}
