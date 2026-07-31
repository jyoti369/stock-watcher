"""In-app financial advisor chat.

Answers questions like "what should I do with PVRINOX?" by mixing three things:

  1. YOUR data — decrypted holdings, mutual funds, the money plan, and the
     buy/sell advice ledger (all read locally, never sent anywhere except the
     model call you trigger).
  2. LIVE market data — current price, day move, fundamentals health and the
     analyst target for any stock the question is about.
  3. FRESH web headlines — reused from ai_insights.gather_news (free Google
     News + Yahoo), so the answer reflects today, not the model's stale memory.

Those are packed into a prompt with a fixed advisor brief (your profile, risk
level, tax slab, and the standing plan) and sent to Gemini/OpenAI. Unlike the
read-only insight layer, this one MAY give a clear stance — that's the point —
but the brief forces honest ranges over promises, plain language over jargon,
and "screenshot before you submit / this is not execution" discipline.

Requires the state key (to read your encrypted data) and an AI key.
"""
from __future__ import annotations

import re
from typing import Any

from . import advice, ai_insights, analysis, finance_plan, mf, watcher
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
    "12.5k, an 8L arbitrage-fund parking that drips into the index via a 10-month "
    "STP, a cleaned-up mutual-fund book, FDs being gifted to parents at maturity, "
    "and a slow stock-portfolio cleanup.\n\n"
    "HOW TO ANSWER:\n"
    "- Lead with a clear stance when asked (keep / trim / sell / wait) — that is "
    "why you exist. But show the reasoning and the risks, never a bare verdict.\n"
    "- Ground every stock claim in the LIVE FIGURES and HEADLINES provided. If the "
    "news is thin or the data is missing, say so — never invent numbers.\n"
    "- Honest projections only: use ranges, never guarantees or precise targets; "
    "say plainly when something is uncertain.\n"
    "- Frame returns after tax when it matters at their 30% slab.\n"
    "- Prefer their existing plan and low-risk lean; discourage churn, hype, and "
    "tips. Contribution matters more than chasing risk.\n"
    "- You ADVISE; you do not execute. For any action, remind them it's their call "
    "and (for trades) to check the order screen before submitting.\n"
    "- Keep it tight and readable: a few short paragraphs, plain American English, "
    "no markdown headers, no em-dashes, no emoji."
)

_NAME_HINTS = {
    "pvr": "PVRINOX", "asian": "ASIANPAINT", "paint": "ASIANPAINT",
    "infy": "INFY", "infosys": "INFY", "cdsl": "CDSL", "idea": "IDEA",
    "vodafone": "IDEA", "cochin": "COCHINSHIP", "graphite": "GRAPHITE",
    "itc": "ITC", "icici": "ICICIBANK", "angel": "ANGELONE",
    "gold": "GOLDBEES", "suzlon": "SUZLON", "bandhan": "BANDHANBNK",
}


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
    syms = set()
    for a in (advice.load_advice() or []):
        syms.add(a["symbol"])
    for w in _read_maybe_enc(WATCHLIST_JSON, []):
        syms.add(w.get("symbol", ""))
    return sorted(s for s in syms if s)


def _money_context() -> str:
    """Compact text pack of everything we hold + the plan + open advice calls."""
    parts = []
    mf_rows = mf.load_mf() or []
    if mf_rows:
        lines = []
        for h in mf_rows:
            nav = mf.latest_nav(str(h["code"])) if h.get("code") else None
            v = mf.value_row(h, nav)
            val = f"Rs {v['value']:,.0f}" if v["value"] else "?"
            lines.append(f"  - {h['name']}: {val} ({v['source']})")
        parts.append("MUTUAL FUNDS:\n" + "\n".join(lines))

    calls = [a for a in (advice.load_advice() or []) if a.get("status", "OPEN") == "OPEN"]
    if calls:
        lines = []
        for a in calls:
            bands = []
            if advice._num(a.get("sell_above")):
                bands.append(f"sell>{advice._num(a['sell_above']):g}")
            if advice._num(a.get("stop_below")):
                bands.append(f"stop<{advice._num(a['stop_below']):g}")
            b = f" [{', '.join(bands)}]" if bands else ""
            lines.append(f"  - {a['symbol']} {a.get('stance')}: {a.get('thesis')}{b}")
        parts.append("CURRENT ADVICE LEDGER (your own prior calls):\n" + "\n".join(lines))

    plan = finance_plan.load_plan()
    if plan and plan.get("content"):
        parts.append("MONEY PLAN (pocket copy):\n" + plan["content"][:2500])
    return "\n\n".join(parts) if parts else "(no stored portfolio data found)"


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


def answer(query: str, history: list[dict] | None = None) -> dict[str, Any]:
    """Return {text, sources, engine} or {error}."""
    avail = ai_insights.available()
    if not (avail["gemini"] or avail["openai"]):
        return {"error": "No AI key configured (STOCKWATCH_GEMINI_KEY)."}
    if finance_plan.load_plan() is None and not (mf.load_mf() or advice.load_advice()):
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

    prompt = (f"{BRIEF}\n\nYOUR DATA\n{_money_context()}{live}\n\n"
              f"CONVERSATION SO FAR:{convo}\n\nUser: {query}\n\n"
              "Answer as their advisor now.")
    engine = "gemini" if avail["gemini"] else "openai"
    try:
        text = ai_insights._gemini(prompt) if engine == "gemini" else ai_insights._openai(prompt)
    except Exception as e:
        return {"error": str(e)[:200]}
    seen, uniq = set(), []
    for n in sources:
        if n["title"] not in seen:
            seen.add(n["title"])
            uniq.append(n)
    return {"text": text, "sources": uniq[:6],
            "engine": ("Gemini" if engine == "gemini" else "OpenAI") + " · your data + live market + news"}
