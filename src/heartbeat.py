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

from . import (advice, alerts, analysis, brand, clock, datasource, db, fmt,
               insights, ipo, mailhtml, portfolio, reminders, settings, watcher)

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
            "pct_change_day": q.get("pct_change"), "atp": q.get("atp"),
            "day_high": q.get("day_high"), "day_low": q.get("day_low")}


def money_report() -> dict:
    """Everything the after-close mail says about money, as data.

    {positions, tail, watch_only, totals, unavailable, unpriced}. Rendered
    twice — text_stock_lines() for Telegram, mailhtml.stock_rows() for the
    email — so neither version can drift into saying something the other
    doesn't.
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
    prefs = settings.load()
    positions = portfolio.by_symbol(lots, sort=prefs.get("sort_by", "value"))
    totals = portfolio.totals(lots) if lots else {}

    unavailable, unpriced = 0, []
    # the top holdings get a full row each; the rest are listed compactly rather
    # than hidden, so nothing you own is invisible. Settings → 0 gives every
    # holding a full row.
    cap = int(prefs.get("positions_shown", _POSITIONS_SHOWN) or len(positions))
    shown = positions[:cap]
    for p in shown:
        p["rating"] = _rating(p["symbol"], watch)
        p["weak"] = p["rating"] == "Weak"
        if p["value"] is None:
            unpriced.append(p["symbol"])
    rest = positions[cap:]
    tail = None
    if rest:
        priced = [p for p in rest if p["value"] is not None]
        unpriced += [p["symbol"] for p in rest if p["value"] is None]
        tail = {"count": len(rest),
                "value": sum(p["value"] for p in priced),
                "pnl": sum(p["pnl"] for p in priced),
                "names": ", ".join(p["symbol"] for p in rest[:8])
                         + (", …" if len(rest) > 8 else ""),
                # every one of them, so "10 smaller holdings" can be read rather
                # than just counted
                "rows": [{"symbol": p["symbol"], "qty": p["qty"],
                          "value": p["value"], "pnl": p["pnl"],
                          "pnl_pct": p["pnl_pct"], "price": p["price"]}
                         for p in rest]}

    held = {p["symbol"] for p in positions}
    watch_only = []
    for (symbol, exchange) in watch:
        if symbol in held:
            continue
        v = vals(symbol, exchange)
        if v.get("price") is None:
            unavailable += 1
        watch_only.append({"symbol": symbol, "price": v.get("price"),
                           "day_pct": v.get("pct_change_day"),
                           "weak": bool(_weak_note(symbol, watch))})
    return {"positions": shown, "tail": tail, "watch_only": watch_only,
            "totals": totals, "unavailable": unavailable, "unpriced": unpriced}


def text_stock_lines(report: dict) -> list[str]:
    """The plain-text stock list (Telegram). Each number carries its own arrow
    or word: the day's move is one thing, your profit on the stock is another,
    and mixing their colours read as a contradiction."""
    lines = []
    for p in report["positions"]:
        if p["value"] is None:
            lines.append(f"{p['symbol']} — no price this run (you hold {p['qty']:g})")
            continue
        lines.append(f"{p['symbol']} {fmt.inr(p['price'])} · today "
                     f"{fmt.move(p['day_pct'])} · you hold {p['qty']:g}, "
                     f"{'up' if p['pnl'] >= 0 else 'down'} {fmt.inr(abs(p['pnl']))} "
                     f"({fmt.pct(p['pnl_pct'])})"
                     + (" · ⚠️ weak fundamentals" if p.get("weak") else ""))
    t = report.get("tail")
    if t:
        lines.append(f"…and {_plural(t['count'], 'smaller holding')} worth "
                     f"{fmt.inr(t['value'])} together, "
                     f"{'up' if t['pnl'] >= 0 else 'down'} {fmt.inr(abs(t['pnl']))} "
                     f"overall ({t['names']})")
    for w in report["watch_only"]:
        if w["price"] is None:
            lines.append(f"{w['symbol']} — no price this run")
            continue
        lines.append(f"{w['symbol']} {fmt.inr(w['price'])} · today "
                     f"{fmt.move(w['day_pct'])} · watching, not held"
                     + (" · ⚠️ weak fundamentals" if w.get("weak") else ""))
    if report["unpriced"]:
        # these are usually broker-statement names ("NIPPON ETF JUNI.") that no
        # exchange recognises — say so, or the gap repeats forever unexplained
        lines.append(f"No live price for {', '.join(report['unpriced'])} — if "
                     f"that's a broker-statement name rather than the NSE symbol, "
                     f"fix it in the app's Portfolio tab and it'll start counting")
    return lines


def _rating(symbol: str, watch: dict) -> str | None:
    """The business-quality band, but only for watchlist names — their
    fundamentals are already fetched this run, and pulling it for every holding
    would triple the job's network use for a word in one line.

    A weak rating is shown as words now: the old coloured 'health' dot sat next
    to a red price and read as "up".
    """
    exchange = next((e for (s, e) in watch if s == symbol), None)
    if exchange is None:
        return None
    try:
        return analysis.score_fundamentals(symbol, exchange).get("rating")
    except Exception:
        return None


def _weak_note(symbol: str, watch: dict) -> str:
    return " · ⚠️ its fundamentals score weak" \
        if _rating(symbol, watch) == "Weak" else ""


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
        item = {"text": r["text"], "date": eff, "ref": reminders.ref(r)}
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

def worth_knowing(report: dict, today: date, limit: int = 1) -> list[dict]:
    """The one thing the app noticed today, for the bottom of the digest.

    Same engine as the app's banner, so the mail and the dashboard can't
    disagree. Off when you've turned digest tips off in Settings; never raises,
    because a tip is the least important thing in the mail.
    """
    if not settings.get("digest_tips"):
        return []
    try:
        # the tail rows count as positions here too, or a suspicious -75% in the
        # small holdings would never be noticed
        every = report["positions"] + list((report.get("tail") or {}).get("rows", []))
        return insights.choose(insights.collect(
            positions=every, tail=report.get("tail"),
            totals=report["totals"], holdings=db.get_holdings(),
            prices={p["symbol"]: p.get("price") for p in every},
            ratings={p["symbol"]: p.get("rating") for p in report["positions"]},
            advice_rows=advice.load_advice() or [],
            rules=db.get_rules(active_only=False),
            history=db.get_alert_history(limit=100),
            reminders_rows=reminders.load() or [], today=today),
            n=limit, seed=today.toordinal())
    except Exception as e:
        print(f"[heartbeat] tips skipped: {str(e)[:120]}")
        return []


def done_link(item: dict) -> str:
    """URL that opens the app and ticks this reminder off. Empty when there's no
    app URL configured or you've switched mail actions off."""
    prefs = settings.load()
    base = str(prefs.get("app_url") or "").rstrip("/")
    if not base or not prefs.get("mail_actions", True) or not item.get("ref"):
        return ""
    return f"{base}/?done={item['ref']}"


def _dated(items: list[dict], today: date, actions: bool = False) -> list[dict]:
    """Reminder items in the shape the HTML renderer wants."""
    out = []
    for i in items:
        row = {"text": i["text"],
               "when": f"was due {clock.when(i['date'], today)}"
                       if i["date"] and i["date"] < today
                       else (clock.when(i["date"], today) if i["date"] else "")}
        if actions:
            row["link"] = done_link(i)
        out.append(row)
    return out


def send_daily() -> list[str]:
    if not db.get_watchlist() and not db.get_holdings():
        print("[heartbeat] nothing watched or held, nothing to send")
        return []
    today = clock.ist_today()
    report = money_report()
    totals = report["totals"]
    head = _header("market still open, so these prices are mid-session",
                   "after the close")
    parts, html_blocks = [head], []

    block = portfolio_block(totals)
    if block:
        parts.append("\n".join(block))
    html_blocks.append(mailhtml.money_card(totals, fmt))
    stock_lines = text_stock_lines(report)
    if stock_lines:
        parts.append("📋 Stock by stock (each number is coloured for itself: "
                     "the day's move, then your own profit)\n"
                     + "\n".join(stock_lines))
        html_blocks.append(mailhtml.section(
            "Stock by stock",
            mailhtml.stock_rows(report["positions"], report["watch_only"],
                                report["tail"], fmt)
            + (mailhtml.section("The smaller holdings",
                                mailhtml.small_holdings(report["tail"], fmt))
               if (report["tail"] or {}).get("rows") else "")
            + (mailhtml.note("No live price for "
                             + ", ".join(report["unpriced"])
                             + " — if that's a broker-statement name rather than "
                               "the NSE symbol, fix it in the Portfolio tab and "
                               "it'll start counting")
               if report["unpriced"] else "")))

    fired = alerts_today_lines()
    if fired:
        parts.append("🔔 Your alerts that fired today\n" + "\n".join(fired))
        html_blocks.append(mailhtml.section(
            "Your alerts that fired today",
            mailhtml.bullets([ln.lstrip("• ") for ln in fired])))

    rem = reminder_buckets(today)
    if rem["overdue"]:
        parts.append("⚠️ Past their date — not marked done\n"
                     + "\n".join(render_overdue(rem["overdue"], today)))
        html_blocks.append(mailhtml.section(
            "Past their date — not marked done",
            mailhtml.dated_items(_dated(rem["overdue"][:_OVERDUE_SHOWN], today,
                                        actions=True), "warn")
            + (mailhtml.note(f"…and {len(rem['overdue']) - _OVERDUE_SHOWN} more "
                             f"past their date")
               if len(rem["overdue"]) > _OVERDUE_SHOWN else ""), "warn"))
    if rem["today"]:
        parts.append(f"📅 Set for today ({clock.short(today)}) — still open\n"
                     + "\n".join(f"• {i['text']}" for i in rem["today"]))
        html_blocks.append(mailhtml.section(
            f"Set for today ({clock.short(today)}) — still open",
            mailhtml.dated_items(_dated(rem["today"], today, actions=True))))
    if rem["upcoming"]:
        parts.append("📆 Coming up\n" + "\n".join(render_upcoming(rem["upcoming"], today)))
        html_blocks.append(mailhtml.section(
            "Coming up", mailhtml.dated_items(_dated(rem["upcoming"], today))))

    rev = review_buckets(today)
    if rev["overdue"] or rev["due"]:
        parts.append("⏰ Advice ledger — calls to revisit\n"
                     + "\n".join(rev["overdue"] + rev["due"]))
        html_blocks.append(mailhtml.section(
            "Advice ledger — calls to revisit",
            mailhtml.bullets([ln.lstrip("• ")
                              for ln in rev["overdue"] + rev["due"]])))

    for t in worth_knowing(report, today):
        parts.append("💡 Worth knowing\n" + insights.as_text(t)
                     + (f"\n{t['action']}" if t.get("action") else ""))
        html_blocks.append(mailhtml.section(
            "Worth knowing",
            mailhtml.dated_items([{"text": t["text"],
                                   "when": " ".join(x for x in (t.get("why"),
                                                                t.get("action")) if x)}])))

    n_rules = len(db.get_rules(active_only=True))
    health = "Watcher is healthy" if report["unavailable"] == 0 else \
        f"⚠️ {_plural(report['unavailable'], 'stock')} had no price this run"
    footer = (f"{health} · {_plural(n_rules, 'alert rule')} armed · "
              f"{len(fired)} fired today.")
    tail_note = ("this arrives every trading day around 3:45 pm. Getting it "
                 "means the watcher is alive, so silence from the alert checker "
                 "really does mean nothing crossed your lines.")
    parts.append(f"{footer}\n({tail_note})")

    subject = f"{brand.DIGEST_SUBJECT} · {clock.short(today)}"
    if totals.get("invested"):
        subject += (f" · today {fmt.signed_inr(totals.get('day_move'))}"
                    f" ({fmt.pct(totals.get('day_pct'))}), overall "
                    f"{fmt.pct(totals.get('pnl_pct'))}")
    else:
        subject += " · daily digest"
    html_body = mailhtml.page(brand.DIGEST_TITLE, head, html_blocks,
                              f"{footer}<br>{tail_note}")
    channels = alerts.dispatch(subject, "\n\n".join(parts), html_body=html_body)
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
    head = _header("the whole afternoon still left to act",
                   "market's already shut, so read this as tomorrow's list")
    body = [head]
    sections: list[str] = []
    html_blocks: list[str] = []

    if todo:
        sections.append("⚡ Do today\n" + "\n".join(
            f"{n}. {t}" for n, t in enumerate(todo, 1)))
        html_blocks.append(mailhtml.section("Do today", mailhtml.todo_cards(todo),
                                           "act"))
    if rem["overdue"]:
        sections.append("⚠️ Past their date — deal with them or clear them\n"
                        + "\n".join(render_overdue(rem["overdue"], today)))
        html_blocks.append(mailhtml.section(
            "Past their date — deal with them or clear them",
            mailhtml.dated_items(_dated(rem["overdue"][:_OVERDUE_SHOWN], today,
                                        actions=True), "warn"), "warn"))
    if rev["overdue"]:
        sections.append("⏰ Advice reviews you're late on\n" + "\n".join(rev["overdue"]))
        html_blocks.append(mailhtml.section(
            "Advice reviews you're late on",
            mailhtml.bullets([ln.lstrip("• ") for ln in rev["overdue"]]), "warn"))
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
        keep = [r for r in ipos["rows"] if r["verdict"] != "SKIP"]
        html_blocks.append(mailhtml.section(
            "IPOs open right now",
            mailhtml.ipo_rows(keep)
            + (mailhtml.note(ipos["skip"]) if ipos["skip"] else "")))
    if drops:
        sections.append("🛒 Tracked prices moved\n" + "\n".join(drops))
        html_blocks.append(mailhtml.section("Tracked prices moved",
                                           mailhtml.bullets(drops)))
    if rem["upcoming"]:
        sections.append("📆 Coming up (nothing to do yet)\n"
                        + "\n".join(render_upcoming(rem["upcoming"], today)))
        html_blocks.append(mailhtml.section(
            "Coming up (nothing to do yet)",
            mailhtml.dated_items(_dated(rem["upcoming"], today))))
    if rev["due"]:
        sections.append("⏰ Advice reviews due\n" + "\n".join(rev["due"]))
        html_blocks.append(mailhtml.section(
            "Advice reviews due",
            mailhtml.bullets([ln.lstrip("• ") for ln in rev["due"]])))

    if not sections:
        print("[heartbeat] morning brief: nothing due, no open IPOs — staying quiet")
        return []
    count = len(todo)
    subject = f"{brand.BRIEF_SUBJECT} · {clock.short(today)} · " + (
        f"{count} thing{'s' if count > 1 else ''} to do today" if count
        else "midday brief")
    html_body = mailhtml.page(brand.BRIEF_TITLE, head, html_blocks,
                              (ipos["footer"] + " Apply on the last day, one lot "
                               "per PAN.") if ipos["footer"] else
                              "Apply on the last day, one lot per PAN.")
    channels = alerts.dispatch(subject, "\n\n".join(body + sections),
                               html_body=html_body)
    db.log_alert(None, "DIGEST", "-", "midday brief", channels)
    print(f"[heartbeat] morning brief sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    send_morning() if "morning" in sys.argv[1:] else send_daily()
