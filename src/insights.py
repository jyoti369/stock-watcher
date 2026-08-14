"""The app noticing things for you.

Everything in here reads across data that otherwise sits in separate tabs —
positions, alert rules, the advice ledger, reminders, fund rows — and turns a
pattern into one sentence with your own numbers in it. That crossing-over is
the point: the Portfolio tab can tell you INFY is down 19%, but only something
looking at the ledger too can notice that a position down 19% has no stop line
set anywhere.

Rules, not a language model: each tip is a small pure function of data you can
read, so it can't invent a number, and a wrong one is a bug you can fix. Each
carries an urgency (0-100) used for ordering, a category so you can switch
whole classes of them off, and a `why` that has to be verifiable from your own
data — a tip you can't check is just noise with confidence.

Nothing here is advice: a tip may point out that a losing position has no
plan, never that you should buy or sell.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import advice, clock, fmt

CATEGORIES = ["risk", "money", "tax", "hygiene", "ipo", "habit"]

# advance-tax instalments (Income-tax Act 2025 s.408): cumulative % of the
# year's estimated tax due by each date
_ADVANCE_TAX = [(6, 15, 15), (9, 15, 45), (12, 15, 75), (3, 15, 100)]
_LTCG_DAYS = 365            # listed equity turns long-term after 12 months
_LTCG_RATE, _STCG_RATE = 12.5, 20.0


def tip(key: str, category: str, urgency: int, text: str, why: str = "",
        action: str = "") -> dict:
    return {"key": key, "category": category, "urgency": max(0, min(100, urgency)),
            "text": text, "why": why, "action": action}


# ---- position-shaped tips ------------------------------------------------

def concentration(positions: list[dict], value: float | None) -> list[dict]:
    """One stock carrying most of the portfolio is the risk nobody notices
    while it's going up."""
    if not value or not positions:
        return []
    top = max((p for p in positions if p.get("value")), key=lambda p: p["value"],
              default=None)
    if not top:
        return []
    share = top["value"] / value * 100
    if share < 30:
        return []
    return [tip("concentration", "risk", 55 + int(share) // 3,
                f"{top['symbol']} is {share:.0f}% of everything you hold "
                f"({fmt.inr(top['value'])} of {fmt.inr(value)}).",
                "One company moving 10% swings your whole portfolio by "
                f"{share / 10:.0f}%.",
                "Nothing to do today — just know that's the single bet you're "
                "carrying.")]


def broken_thesis(positions: list[dict], ratings: dict) -> list[dict]:
    """Down a lot AND weak on fundamentals is the combination worth a second
    look — a falling price alone says nothing."""
    out = []
    for p in positions:
        if p.get("pnl_pct") is None or p["pnl_pct"] > -20:
            continue
        if ratings.get(p["symbol"]) != "Weak":
            continue
        out.append(tip(f"broken:{p['symbol']}", "risk", 72,
                       f"{p['symbol']} is down {abs(p['pnl_pct']):.0f}% and its "
                       f"business score reads Weak.",
                       "Price falling is noise on its own; falling while the "
                       "fundamentals score weak is the pair worth re-reading.",
                       f"Open Stock analysis → {p['symbol']} and read the bear "
                       f"case before adding to it."))
    return out


def unprotected_losers(positions: list[dict], advice_rows: list[dict],
                       rules: list[dict]) -> list[dict]:
    """A losing position with no stop line anywhere — not in the ledger, not in
    the alert rules."""
    stops = {a["symbol"] for a in advice_rows or []
             if a.get("status", "OPEN") == "OPEN" and a.get("stop_below")}
    alerted = {r["symbol"] for r in rules or [] if r.get("active")
               and any(c.get("metric") == "price" and "<" in str(c.get("op"))
                       for c in r.get("conditions", []))}
    out = []
    for p in positions:
        if p.get("pnl_pct") is None or p["pnl_pct"] > -10:
            continue
        if p["symbol"] in stops or p["symbol"] in alerted:
            continue
        out.append(tip(f"nostop:{p['symbol']}", "risk", 60,
                       f"{p['symbol']} is down {abs(p['pnl_pct']):.0f}% "
                       f"({fmt.inr(abs(p['pnl'] or 0))}) with no stop line set.",
                       "Nothing in the ledger or your alert rules would tell you "
                       "if it kept falling.",
                       "Advice tab → set a stop-below, then Arm all exit alerts."))
    return out[:2]


def unplanned_winners(positions: list[dict], advice_rows: list[dict]) -> list[dict]:
    """A big gain with no exit band is a decision you'll end up making in a
    hurry."""
    exits = {a["symbol"] for a in advice_rows or []
             if a.get("status", "OPEN") == "OPEN" and a.get("sell_above")}
    out = []
    for p in positions:
        if p.get("pnl_pct") is None or p["pnl_pct"] < 20 or p["symbol"] in exits:
            continue
        out.append(tip(f"noexit:{p['symbol']}", "money", 45,
                       f"{p['symbol']} is up {p['pnl_pct']:.0f}% "
                       f"({fmt.inr(p['pnl'])}) and has no sell price written down.",
                       "Gains you haven't planned an exit for tend to get sold on "
                       "a bad day instead of a good one.",
                       "Advice tab → set a sell-above band so the watcher pings "
                       "you at your number."))
    return out[:2]


def loss_is_concentrated(positions: list[dict], totals: dict) -> list[dict]:
    """Where the damage actually is, when the headline number hides it."""
    down = [p for p in positions if (p.get("pnl") or 0) < 0]
    losers = sorted(down, key=lambda p: p["pnl"])[:3]
    if not losers or not totals.get("pnl") or totals["pnl"] >= 0:
        return []
    # share of the LOSSES, not of the net figure — the winners offsetting them
    # is what produced "105% of your total loss" here once
    all_losses = sum(p["pnl"] for p in down)
    if not all_losses:
        return []
    share = sum(p["pnl"] for p in losers) / all_losses * 100
    if share < 60 or len(positions) < 4:
        return []
    names = ", ".join(f"{p['symbol']} {fmt.pct(p['pnl_pct'])}" for p in losers)
    return [tip("loss_concentrated", "money", 40,
                f"{share:.0f}% of everything you're down sits in "
                f"{len(losers)} stocks: {names}.",
                "The portfolio-level percentage averages your winners and losers "
                "together, which hides where the money actually went.",
                "Worth re-reading those three specifically rather than the "
                "whole list.")]


def suspect_cost_basis(positions: list[dict]) -> list[dict]:
    """A loss too big to be a loss.

    ITCHOTELS showing -75% and PRSMJOHNSN -53% in the same portfolio isn't the
    market; it's a demerger and a split where the price adjusted and the
    recorded buy price didn't. Worth saying, because every total above it is
    wrong by the same amount.
    """
    odd = [p for p in positions
           if p.get("pnl_pct") is not None and p["pnl_pct"] < -60]
    if not odd:
        return []
    names = ", ".join(f"{p['symbol']} {fmt.pct(p['pnl_pct'])}" for p in odd[:3])
    return [tip("costbasis", "hygiene", 58,
                f"{names} — a loss that size is usually not a loss.",
                "A demerger, split or bonus moves the price without changing the "
                "buy price your statement recorded, so the app compares today's "
                "adjusted price against an unadjusted cost.",
                "Portfolio tab → Manage holdings, correct the buy price to the "
                "post-event one; every total gets more honest.")]


def attention_tail(positions: list[dict], tail: dict | None) -> list[dict]:
    if not tail or tail.get("count", 0) < 6:
        return []
    return [tip("tail", "habit", 25,
                f"{tail['count']} holdings add up to {fmt.inr(tail['value'])} "
                f"between them.",
                "Each one still costs you a line to read every day, and a 5% move "
                "on any of them changes almost nothing.",
                "No action needed — the digest already rolls them into one line.")]


# ---- tax ------------------------------------------------------------------

def ltcg_countdown(holdings: list[dict], prices: dict,
                   today: date | None = None) -> list[dict]:
    """Days until a holding crosses 12 months, where that changes the tax rate
    on a gain you already have."""
    today = today or clock.ist_today()
    out = []
    for h in holdings or []:
        raw = h.get("buy_date")
        if not raw:
            continue
        try:
            bought = date.fromisoformat(str(raw)[:10])
        except (ValueError, TypeError):
            continue
        crosses = bought + timedelta(days=_LTCG_DAYS)
        days = (crosses - today).days
        if not 0 < days <= 45:
            continue
        price = prices.get(h["symbol"])
        gain = (price - h["buy_price"]) * h["qty"] if price else None
        if gain is None or gain <= 0:
            continue
        saving = gain * (_STCG_RATE - _LTCG_RATE) / 100
        out.append(tip(f"ltcg:{h['symbol']}", "tax", 65,
                       f"{h['symbol']} turns long-term in {days} day"
                       f"{'' if days == 1 else 's'} "
                       f"({clock.short(crosses)}).",
                       f"Selling before then taxes the {fmt.inr(gain)} gain at "
                       f"{_STCG_RATE:g}% instead of {_LTCG_RATE:g}% — about "
                       f"{fmt.inr(saving)} of difference.",
                       "Nothing to do unless you were about to sell it."))
    return out


def missing_buy_dates(holdings: list[dict]) -> list[dict]:
    n = sum(1 for h in holdings or [] if not h.get("buy_date"))
    if not n:
        return []
    which = (f"None of your {len(holdings)} holdings have a purchase date"
             if n == len(holdings) else
             f"{n} of your {len(holdings)} holdings have no purchase date")
    return [tip("nodates", "hygiene", 35, which + ".",
                "Without dates the app can't tell you when a holding crosses the "
                "12-month line, where the tax on a gain drops from "
                f"{_STCG_RATE:g}% to {_LTCG_RATE:g}%.",
                "Portfolio tab → Manage holdings, or re-import a broker statement "
                "that includes dates.")]


def advance_tax(today: date | None = None) -> list[dict]:
    """The instalment dates, surfaced only when one is close."""
    today = today or clock.ist_today()
    for month, day, cum in _ADVANCE_TAX:
        year = today.year + (1 if month < today.month - 6 else 0)
        due = date(year, month, day)
        gap = (due - today).days
        if 0 <= gap <= 21:
            return [tip(f"advtax:{due}", "tax", 55 + (21 - gap),
                        f"Advance tax instalment due {clock.when(due, today)} — "
                        f"{cum}% of the year's estimated tax by then.",
                        "Capital gains carry no TDS, so tax on anything you sold "
                        "stays unpaid until you pay it yourself.",
                        "Plan tab → check the estimate, then pay on the income "
                        "tax portal.")]
    return []


# ---- plumbing that has gone quiet ----------------------------------------

def stale_rules(rules: list[dict], history: list[dict],
                today: date | None = None) -> list[dict]:
    """Rules that have never fired in a long time are usually set too far away
    to ever be useful."""
    today = today or clock.ist_today()
    fired = set()
    for h in history or []:
        when = clock.to_ist(h.get("ts", ""))
        if when and (today - when.date()).days <= 120:
            fired.add(h.get("symbol"))
    quiet = [r for r in rules or [] if r.get("active") and r["symbol"] not in fired]
    if len(quiet) < 3:
        return []
    return [tip("stalerules", "hygiene", 30,
                f"{len(quiet)} of your alert rules haven't fired in four months.",
                "A line the price never comes near is the same as no alert at "
                "all — you feel covered without being covered.",
                "Alerts tab → each rule shows how far it is from firing; move the "
                "far ones closer or pause them.")]


def nagging_rule(rules: list[dict], today: date | None = None) -> list[dict]:
    """A standing rule that has been true for days is no longer information."""
    today = today or clock.ist_today()
    out = []
    for r in rules or []:
        if not r.get("active") or r.get("mode") != "level" or not r.get("true_since"):
            continue
        try:
            since = date.fromisoformat(str(r["true_since"])[:10])
        except (ValueError, TypeError):
            continue
        days = (today - since).days
        if days < 5:
            continue
        out.append(tip(f"nag:{r['id']}", "hygiene", 45,
                       f"“{r.get('label') or 'a rule'}” on {r['symbol']} has been "
                       f"true for {days} days straight.",
                       "You're getting a mail a day that says the same thing, "
                       "which is how you learn to ignore the mails that matter.",
                       "Alerts tab → pause it, or move the level so it means "
                       "something again."))
    return out[:1]


def overdue_work(reminders_rows: list[dict], advice_rows: list[dict],
                 today: date | None = None) -> list[dict]:
    from . import reminders as rem_mod
    today = today or clock.ist_today()
    late = 0
    for r in reminders_rows or []:
        eff = rem_mod.effective_date(r, today)
        if rem_mod.due(r, today) and eff and eff < today:
            late += 1
    reviews = sum(1 for a in advice_rows or [] if advice.due_soon(a, today)
                  and _before_today(a, today))
    out = []
    if late:
        out.append(tip("overdue", "habit", 50 + late * 5,
                       f"{late} reminder{'' if late == 1 else 's'} sat past their "
                       f"date without being marked done.",
                       "Every digest repeats them, so they stop reading as jobs "
                       "and start reading as furniture.",
                       "Plan tab → tick them off, or move the date if they're "
                       "still real."))
    if reviews:
        out.append(tip("reviews", "habit", 45,
                       f"{reviews} advice call{'' if reviews == 1 else 's'} passed "
                       f"the review date you set for {'it' if reviews == 1 else 'them'}.",
                       "You wrote the date down because the reasoning had a shelf "
                       "life.",
                       "Advice tab → re-read the thesis and either close it or "
                       "push the date."))
    return out


def _before_today(entry: dict, today: date) -> bool:
    raw = entry.get("catalyst_date") or entry.get("review_by")
    try:
        return date.fromisoformat(str(raw)[:10]) < today
    except (ValueError, TypeError):
        return False


def data_gaps(positions: list[dict], mf_rows: list[dict]) -> list[dict]:
    out = []
    blind = [p["symbol"] for p in positions if p.get("value") is None]
    if blind:
        out.append(tip("unpriced", "hygiene", 40,
                       f"No exchange recognises {', '.join(blind[:3])}, so "
                       f"{'it sits' if len(blind) == 1 else 'they sit'} outside "
                       f"every total.",
                       "These are broker-statement names rather than NSE symbols.",
                       "Portfolio tab → Manage holdings, replace with the traded "
                       "symbol."))
    estimates = sum(1 for r in mf_rows or [] if not r.get("units")
                    or not r.get("invested"))
    if estimates:
        out.append(tip("mfgaps", "hygiene", 28,
                       f"{estimates} fund row{'' if estimates == 1 else 's'} "
                       f"still missing units or cost.",
                       "Without both, the fund's profit is a guess and the total "
                       "at the top is understated.",
                       "Your monthly CAS email (CAMS/KFintech) has exact units "
                       "for every fund you own."))
    return out


# ---- house knowledge, only when nothing needs doing -----------------------

_HOUSE = [
    ("gmp_vs_qib", "ipo", "Grey-market premium is a quote from an unofficial "
     "market and is easy to inflate, especially on SME issues. Institutional "
     "(QIB) subscription is the honesty check, because institutions don't bid "
     "into a rigged book."),
    ("last_day", "ipo", "Allotment odds don't depend on when you bid, so "
     "applying on the last day is free information — you get to see the final "
     "book and where the premium settled."),
    ("one_lot", "ipo", "In an oversubscribed retail book, extra lots don't raise "
     "your odds — allotment is one lot per PAN by lottery. Size for that."),
    ("silence", "habit", "The alert watcher only mails when a rule matches, and "
     "the daily digest arrives regardless. That's deliberate: if the digest "
     "shows up, silence from the watcher genuinely means nothing triggered."),
    ("brief_quiet", "habit", "The 12:05 brief stays silent on days with nothing "
     "due and no open IPOs, so its arrival is itself the signal."),
    ("book_vs_premium", "ipo", "A heavy book with a thin premium is a crowded "
     "queue for a small prize: oversubscription cuts your allotment odds while "
     "the premium says what the prize is worth."),
    ("gains_are_taxed", "tax", f"Listed-equity gains are taxed at "
     f"{_STCG_RATE:g}% under 12 months and {_LTCG_RATE:g}% after, with the "
     f"first ₹1.25 lakh of long-term gains each year exempt — the exemption "
     f"resets every year and doesn't carry forward."),
]


def house_notes() -> list[dict]:
    return [tip(k, c, 12, t) for k, c, t in _HOUSE]


# ---- assembling and choosing --------------------------------------------

def collect(*, positions=None, tail=None, totals=None, holdings=None, prices=None,
            ratings=None, advice_rows=None, rules=None, history=None,
            reminders_rows=None, mf_rows=None, today=None) -> list[dict]:
    """Every tip that currently applies, most urgent first."""
    positions = positions or []
    totals = totals or {}
    tips = []
    tips += concentration(positions, totals.get("value"))
    tips += broken_thesis(positions, ratings or {})
    tips += unprotected_losers(positions, advice_rows or [], rules or [])
    tips += unplanned_winners(positions, advice_rows or [])
    tips += loss_is_concentrated(positions, totals)
    tips += suspect_cost_basis(positions)
    tips += attention_tail(positions, tail)
    tips += ltcg_countdown(holdings or [], prices or {}, today)
    tips += missing_buy_dates(holdings or [])
    tips += advance_tax(today)
    tips += stale_rules(rules or [], history or [], today)
    tips += nagging_rule(rules or [], today)
    tips += overdue_work(reminders_rows or [], advice_rows or [], today)
    tips += data_gaps(positions, mf_rows or [])
    tips += house_notes()
    tips.sort(key=lambda t: -t["urgency"])
    return tips


def choose(tips: list[dict], n: int = 2, seed: int = 0,
           categories: list[str] | None = None, min_urgency: int = 0) -> list[dict]:
    """Pick what to show: anything urgent always makes it, the rest rotates.

    Deterministic for a given seed so a Streamlit rerun doesn't reshuffle the
    banner under the user's eyes — pass a new seed to deliberately rotate.
    """
    pool = [t for t in tips if t["urgency"] >= min_urgency
            and (categories is None or t["category"] in categories)]
    if not pool:
        return []
    urgent = [t for t in pool if t["urgency"] >= 70][:n]
    rest = [t for t in pool if t not in urgent]
    slots = n - len(urgent)
    if slots > 0 and rest:
        # weight by urgency so a real finding beats a house note, without ever
        # locking the low-urgency ones out entirely
        rng = random.Random(seed)
        weights = [max(1, t["urgency"]) for t in rest]
        picked = []
        for _ in range(min(slots, len(rest))):
            choice = rng.choices(range(len(rest)), weights=weights, k=1)[0]
            picked.append(rest.pop(choice))
            weights.pop(choice)
        urgent += picked
    return urgent[:n]


def as_text(t: dict) -> str:
    """One-line form for the digest mails."""
    return t["text"] + (f" {t['why']}" if t.get("why") else "")
