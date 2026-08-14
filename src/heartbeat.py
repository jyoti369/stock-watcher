"""The two daily mails.

Both open with the full date and time in IST, because a mail that says "due
today" has to prove which day it thinks it is — and these run on GitHub's UTC
machines, so "today" is always resolved through clock.ist_today().

The after-close one (~3:45pm IST) is the money summary: what your holdings are
worth, what today did to them, which of your own alerts fired, and what dates
are open. Also a trust signal: if the watcher breaks or a data source goes
dark you see it here, so silence from the alert checker means "nothing
triggered", not "it died quietly".

The midday one (~12:05pm IST) is the action brief, timed so there's still an
afternoon left to act. It leads with a numbered "do today" list — anything with
a same-day deadline, so a live IPO decision can't sit four sections below a
stale reminder — then overdue items, then detail. Silent on days with nothing
to do and no open IPOs.
"""
from __future__ import annotations

import sys
from datetime import date

from . import (advice, alerts, analysis, clock, datasource, db, fmt, ipo,
               portfolio, reminders, watcher)

_OVERDUE_SHOWN = 4          # older ones get counted, not listed
_POSITIONS_SHOWN = 12       # the rest roll into one summary line


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _header(note_before_close: str, note_after: str) -> str:
    """Dated header line, honest about the hour it actually ran at — the mails
    are scheduled but can also be triggered by hand at any time."""
    now = clock.ist_now()
    closed = (now.hour, now.minute) >= (15, 30)
    return f"{clock.stamp(now)} — {note_after if closed else note_before_close}"


# ---- market + money ------------------------------------------------------

def _light_values(symbol: str, exchange: str) -> dict:
    """Just price and today's move — for held symbols that aren't on the
    watchlist, where a full metrics+fundamentals pull isn't worth the
    yfinance quota."""
    q = datasource.get_live_quote(symbol, exchange)
    return {"price": q.get("price") if q.get("ok") else None,
            "pct_change_day": q.get("pct_change")}


def money_report() -> dict:
    """Everything the after-close mail says about money.

    {lines, totals, unavailable} — one line per stock you hold or watch, and
    the portfolio totals. The colour on each line is your own profit or loss
    on that stock (green = up, red = down); for stocks you only watch there's
    no money in it, so the line says so instead of pretending.
    """
    watch = {(w["symbol"], w["exchange"]): w for w in db.get_watchlist()}
    holdings = db.get_holdings()
    values: dict[tuple[str, str], dict] = {}

    def vals(symbol: str, exchange: str) -> dict:
        key = (symbol, exchange)
        if key not in values:
            values[key] = watcher.gather_values(symbol, exchange) \
                if key in watch else _light_values(symbol, exchange)
        return values[key]

    lots = [portfolio.lot_row(h, vals(h["symbol"], h.get("exchange") or "NSE"))
            for h in holdings]
    positions = portfolio.by_symbol(lots)
    totals = portfolio.totals(lots) if lots else {}

    lines, unavailable, unpriced = [], 0, []
    # biggest holdings get a line each; the long tail of small ones is one line,
    # so a 20-position list doesn't bury what actually moves your money
    for p in positions[:_POSITIONS_SHOWN]:
        v = f"{p['symbol']} {fmt.inr(p['price'])}" if p["price"] is not None \
            else p["symbol"]
        if p["value"] is None:
            unpriced.append(p["symbol"])
            lines.append(f"⚪ {p['symbol']} — no price this run "
                         f"(you hold {p['qty']:g})")
            continue
        word = "up" if (p["pnl"] or 0) >= 0 else "down"
        lines.append(f"{fmt.money_dot(p['pnl'])} {v} (today {fmt.move(p['day_pct'])})"
                     f" — you hold {p['qty']:g}, {word} {fmt.inr(abs(p['pnl']))} "
                     f"({fmt.pct(p['pnl_pct'])})" + _weak_note(p["symbol"], watch))
    tail = positions[_POSITIONS_SHOWN:]
    if tail:
        priced = [p for p in tail if p["value"] is not None]
        unpriced += [p["symbol"] for p in tail if p["value"] is None]
        worth = sum(p["value"] for p in priced)
        pnl = sum(p["pnl"] for p in priced)
        lines.append(f"{fmt.money_dot(pnl)} …and {_plural(len(tail), 'smaller holding')}"
                     f" worth {fmt.inr(worth)} together, "
                     f"{'up' if pnl >= 0 else 'down'} {fmt.inr(abs(pnl))} overall "
                     f"({', '.join(p['symbol'] for p in tail[:8])}"
                     f"{', …' if len(tail) > 8 else ''})")

    held = {p["symbol"] for p in positions}
    for (symbol, exchange) in watch:
        if symbol in held:
            continue
        v = vals(symbol, exchange)
        if v.get("price") is None:
            unavailable += 1
            lines.append(f"⚪ {symbol} — no price this run")
            continue
        lines.append(f"▪️ {symbol} {fmt.inr(v['price'])} "
                     f"(today {fmt.move(v.get('pct_change_day'))}) — watching, "
                     f"not held" + _weak_note(symbol, watch))
    if unpriced:
        # these are usually broker-statement names ("NIPPON ETF JUNI.") that no
        # exchange recognises — say so, or the ⚪ repeats forever unexplained
        lines.append(f"⚪ no live price for {', '.join(unpriced)} — if that's a "
                     f"broker-statement name rather than the NSE symbol, fix it "
                     f"in the app's Portfolio tab and it'll start counting")
    return {"lines": lines, "totals": totals, "unavailable": unavailable,
            "unpriced": unpriced}


def _weak_note(symbol: str, watch: dict) -> str:
    """A words-only warning when the business itself looks shaky. Only for
    watchlist names, whose fundamentals we've already fetched this run — the
    old coloured 'health' dot sat next to a red price and read as "up"."""
    exchange = next((e for (s, e) in watch if s == symbol), None)
    if exchange is None:
        return ""
    try:
        rating = analysis.score_fundamentals(symbol, exchange).get("rating")
    except Exception:
        return ""
    return " · ⚠️ its fundamentals score weak" if rating == "Weak" else ""


def portfolio_block(totals: dict) -> list[str]:
    """The top-of-mail money summary, in rupees before percentages."""
    if not totals or not totals.get("invested"):
        return []
    out = ["💼 Your money",
           f"Worth {fmt.inr(totals['value'])} now · you put in "
           f"{fmt.inr(totals['invested'])}",
           f"Today: {fmt.money_dot(totals.get('day_move'))} "
           f"{fmt.signed_inr(totals.get('day_move'))} "
           f"({fmt.pct(totals.get('day_pct'))})",
           f"Since you bought: {fmt.money_dot(totals.get('pnl'))} "
           f"{fmt.signed_inr(totals.get('pnl'))} ({fmt.pct(totals.get('pnl_pct'))})"]
    if totals.get("missing"):
        out.append(f"({_plural(totals['missing'], 'holding')} had no price this "
                   f"run, so they're left out of these totals)")
    return out


def alerts_today_lines() -> list[str]:
    """Recap of your own alerts that fired today, with IST clock times."""
    today = clock.ist_today()
    out = []
    for h in db.get_alert_history(limit=100):
        if h["symbol"] == "DIGEST":
            continue
        when = clock.to_ist(h["ts"])
        if not when or when.date() != today:
            continue
        msg = str(h["message"]).split(" — ")[0].split(": ")[0][:70]
        out.append(f"• {clock.clock_time(when)} — {h['symbol']}: {msg}")
    return list(reversed(out))


# ---- dated things: reminders and advice reviews ---------------------------

def reminder_buckets(today: date | None = None) -> dict[str, list[dict]]:
    """Due reminders split three ways: {overdue, today, upcoming}, each item
    {text, date}. Split rather than one "Due:" list because a line dated
    tomorrow under that header reads as today's job, and a line dated last week
    reads as a fresh one. Empty without the state key."""
    rows = reminders.load()
    today = today or clock.ist_today()
    out: dict[str, list[dict]] = {"overdue": [], "today": [], "upcoming": []}
    for r in rows or []:
        if not reminders.due(r, today):
            continue
        eff = reminders.effective_date(r, today)
        item = {"text": r["text"], "date": eff}
        bucket = "today" if (eff is None or eff == today) \
            else "overdue" if eff < today else "upcoming"
        out[bucket].append(item)
    out["overdue"].sort(key=lambda i: i["date"] or today, reverse=True)
    out["upcoming"].sort(key=lambda i: i["date"] or today)
    return out


def render_overdue(items: list[dict], today: date | None = None) -> list[str]:
    today = today or clock.ist_today()
    out = [f"• {i['text']}\n  was due {clock.when(i['date'], today)}"
           for i in items[:_OVERDUE_SHOWN]]
    extra = len(items) - _OVERDUE_SHOWN
    if extra > 0:
        out.append(f"• …and {extra} more past their date — clear them in the "
                   f"app's Reminders tab")
    return out


def render_upcoming(items: list[dict], today: date | None = None) -> list[str]:
    today = today or clock.ist_today()
    return [f"• {clock.when(i['date'], today)} — {i['text']}" for i in items]


def review_buckets(today: date | None = None) -> dict[str, list[str]]:
    """Advice-ledger calls whose catalyst or review date has arrived, split
    into overdue and due-now lines. Empty when this run has no state key."""
    rows = advice.load_advice()
    today = today or clock.ist_today()
    verb = {"SELL": "sell decision due", "TRIM": "trim due",
            "HOLD-RULE": "decision due"}
    out: dict[str, list[str]] = {"overdue": [], "due": []}
    for a in rows or []:
        if not advice.due_soon(a, today):
            continue
        raw = a.get("catalyst_date") or a.get("review_by")
        v = verb.get(a.get("stance"), "re-examine")
        try:
            d = date.fromisoformat(str(raw)[:10])
        except (ValueError, TypeError):
            out["due"].append(f"• {a['symbol']}: {v}"
                              + (f" (by {advice.pretty_date(raw)})" if raw else ""))
            continue
        line = f"• {a['symbol']}: {v} — {clock.when(d, today)}"
        out["overdue" if d < today else "due"].append(line)
    return out


# ---- the two mails -------------------------------------------------------

def send_daily() -> list[str]:
    if not db.get_watchlist() and not db.get_holdings():
        print("[heartbeat] nothing watched or held, nothing to send")
        return []
    today = clock.ist_today()
    report = money_report()
    totals = report["totals"]
    parts = [_header("market still open, so these prices are mid-session",
                     "after the close")]

    block = portfolio_block(totals)
    if block:
        parts.append("\n".join(block))
    if report["lines"]:
        parts.append("📋 Stock by stock (🟢 = you're in profit on it, 🔴 = in "
                     "loss, ▪️ = only watching)\n" + "\n".join(report["lines"]))

    fired = alerts_today_lines()
    if fired:
        parts.append("🔔 Your alerts that fired today\n" + "\n".join(fired))

    rem = reminder_buckets(today)
    if rem["overdue"]:
        parts.append("⚠️ Past their date — not marked done\n"
                     + "\n".join(render_overdue(rem["overdue"], today)))
    if rem["today"]:
        parts.append(f"📅 Set for today ({clock.short(today)}) — still open\n"
                     + "\n".join(f"• {i['text']}" for i in rem["today"]))
    if rem["upcoming"]:
        parts.append("📆 Coming up\n" + "\n".join(render_upcoming(rem["upcoming"], today)))

    rev = review_buckets(today)
    if rev["overdue"] or rev["due"]:
        parts.append("⏰ Advice ledger — calls to revisit\n"
                     + "\n".join(rev["overdue"] + rev["due"]))

    n_rules = len(db.get_rules(active_only=True))
    health = "Watcher is healthy" if report["unavailable"] == 0 else \
        f"⚠️ {_plural(report['unavailable'], 'stock')} had no price this run"
    parts.append(f"{health} · {_plural(n_rules, 'alert rule')} armed · "
                 f"{len(fired)} fired today.\n"
                 "(this arrives every trading day around 3:45 pm. Getting it "
                 "means the watcher is alive, so silence from the alert "
                 "checker really does mean nothing crossed your lines.)")

    subject = f"📊 Stock Watcher · {clock.short(today)}"
    if totals.get("invested"):
        subject += (f" · today {fmt.signed_inr(totals.get('day_move'))}"
                    f" ({fmt.pct(totals.get('day_pct'))}), overall "
                    f"{fmt.pct(totals.get('pnl_pct'))}")
    else:
        subject += " · daily digest"
    channels = alerts.dispatch(subject, "\n\n".join(parts))
    db.log_alert(None, "DIGEST", "-",
                 f"daily digest ({report['unavailable']} unavailable)", channels)
    print(f"[heartbeat] sent to {channels or 'no channel configured'}")
    return channels


def send_morning() -> list[str]:
    """Midday action brief. Skips the send entirely when there's nothing to act
    on — no noise, only signal."""
    today = clock.ist_today()
    rem = reminder_buckets(today)
    rev = review_buckets(today)
    try:
        ipos = ipo.brief(today)
    except Exception as e:                      # a broken scrape shouldn't kill the brief
        ipos = {"act": [], "watch": [], "skip": None, "todo": [],
                "footer": f"(IPO screener errored: {str(e)[:80]})"}
    try:
        from . import shop_watch
        drops = shop_watch.digest_lines()
    except Exception as e:                      # store blocks shouldn't kill the brief
        drops = [f"(price tracker errored: {str(e)[:80]})"]

    todo = list(ipos["todo"]) + [i["text"] for i in rem["today"]]
    body = [_header("the whole afternoon still left to act",
                    "market's already shut, so read this as tomorrow's list")]
    sections: list[str] = []

    if todo:
        sections.append("⚡ Do today\n" + "\n".join(
            f"{n}. {t}" for n, t in enumerate(todo, 1)))
    if rem["overdue"]:
        sections.append("⚠️ Past their date — deal with them or clear them\n"
                        + "\n".join(render_overdue(rem["overdue"], today)))
    if rev["overdue"]:
        sections.append("⏰ Advice reviews you're late on\n" + "\n".join(rev["overdue"]))
    if ipos["act"] or ipos["watch"] or ipos["skip"]:
        block = ["🎯 IPOs open right now"]
        if ipos["act"]:
            block.append("Passes every bar:\n" + "\n".join("• " + a for a in ipos["act"]))
        if ipos["watch"]:
            block.append("Premium is there, book still filling:\n"
                         + "\n".join("• " + w for w in ipos["watch"]))
        if ipos["skip"]:
            block.append(ipos["skip"])
        block.append(ipos["footer"])
        sections.append("\n".join(block))
    if drops:
        sections.append("🛒 Tracked prices moved\n" + "\n".join(drops))
    if rem["upcoming"]:
        sections.append("📆 Coming up (nothing to do yet)\n"
                        + "\n".join(render_upcoming(rem["upcoming"], today)))
    if rev["due"]:
        sections.append("⏰ Advice reviews due\n" + "\n".join(rev["due"]))

    if not sections:
        print("[heartbeat] morning brief: nothing due, no open IPOs — staying quiet")
        return []
    count = len(todo)
    subject = f"🌅 Stock Watcher · {clock.short(today)} · " + (
        f"{count} thing{'s' if count > 1 else ''} to do today" if count
        else "midday brief")
    channels = alerts.dispatch(subject, "\n\n".join(body + sections))
    db.log_alert(None, "DIGEST", "-", "midday brief", channels)
    print(f"[heartbeat] morning brief sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    send_morning() if "morning" in sys.argv[1:] else send_daily()
