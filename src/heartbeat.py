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
               insights, ipo, mailhtml, portfolio, reminders, settings, tg,
               watcher)

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
                     f"{fmt.move(p['day_pct'])} · you hold {p['qty']:g} "
                     f"worth {fmt.inr(p['value'])}, "
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
        lines.append(f"No price for {', '.join(report['unpriced'])} — fix the "
                     f"symbol in Portfolio.")
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


def holdings_caveat(count: int, today: date | None = None) -> str:
    """One line owning up to how old the holdings list is.

    Every figure above it is computed from that list, and nothing tells this app
    when a stock is sold — so a stale list quietly inflates the total. Better to
    say it than to present the number as fact.
    """
    if not count:
        return ""
    age = settings.holdings_age(today)
    if age is None:
        return ("These figures assume the imported holdings are still yours — "
                "that has never been confirmed, so anything sold is still "
                "counted. Confirm the list in the app's Portfolio tab.")
    if age >= 10:
        return (f"These figures assume the holdings list you last confirmed "
                f"{age} days ago. Sold anything since? It's still being counted.")
    return ""


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

def tg_stock_block(report: dict) -> str:
    """The holdings as a monospace table: name, what it's worth, your P&L, today.

    Four short columns beat one wrapped sentence per stock on a phone — the
    prose version was three lines each and read as a wall.
    """
    rows = []
    for p in report["positions"]:
        rows.append([tg.short_symbol(p["symbol"]),
                     tg.money(p["value"]), tg.pct(p["pnl_pct"]),
                     tg.pct(p["day_pct"])])
    out = tg.table(["Stock", "Worth", "P&L", "Today"], rows)
    t = report.get("tail")
    if t and t.get("rows"):
        inner = tg.table(["Stock", "Worth", "P&L"],
                         [[tg.short_symbol(r["symbol"]), tg.money(r["value"]),
                           tg.pct(r["pnl_pct"])] for r in t["rows"]])
        out += "\n" + tg.collapsed(
            f"…and {t['count']} smaller, {fmt.inr(t['value'])} together "
            f"({fmt.pct((t['pnl'] / (t['value'] - t['pnl']) * 100) if t['value'] != t['pnl'] else 0)})",
            inner)
    watching = [w for w in report["watch_only"] if w.get("price") is not None]
    if watching:
        out += "\n" + tg.b("Watching") + "\n" + tg.bullets(
            [f"{w['symbol']} {fmt.inr(w['price'])} ({fmt.pct(w['day_pct'])})"
             for w in watching])
    return out


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
            holdings_age=settings.holdings_age(today),
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
    # Telegram gets its own build: bold headers, monospace columns, an
    # expandable block for the small holdings, and buttons at the end
    tg_parts = [f"{tg.b(brand.NAME + ' · ' + clock.short(today))}\n"
                f"<i>{tg.esc(head.split(' — ')[-1])}</i>"]
    tg_keys: list[tuple[str, str]] = []

    caveat = holdings_caveat(len(report["positions"]) + len(
        (report.get("tail") or {}).get("rows", [])), today)
    block = portfolio_block(totals)
    if block:
        parts.append("\n".join(block) + (f"\n⚠️ {caveat}" if caveat else ""))
    html_blocks.append(mailhtml.money_card(totals, fmt)
                       + (mailhtml.note("⚠️ " + caveat) if caveat else ""))
    if totals.get("invested"):
        tg_parts.append(
            f"{tg.b('Worth ' + fmt.inr(totals['value']))}  "
            f"<i>(put in {tg.esc(fmt.inr(totals['invested']))})</i>\n"
            f"Today {fmt.money_dot(totals.get('day_move'))} "
            f"{tg.esc(fmt.signed_inr(totals.get('day_move')))} "
            f"({tg.esc(fmt.pct(totals.get('day_pct')))})\n"
            f"Overall {fmt.money_dot(totals.get('pnl'))} "
            f"{tg.esc(fmt.signed_inr(totals.get('pnl')))} "
            f"({tg.esc(fmt.pct(totals.get('pnl_pct')))})"
            + (f"\n<i>⚠️ {tg.esc(caveat)}</i>" if caveat else ""))
    stock_lines = text_stock_lines(report)
    if stock_lines:
        parts.append("📋 Stock by stock\n" + "\n".join(stock_lines))
        tg_parts.append(tg_stock_block(report))
        html_blocks.append(mailhtml.section(
            "Stock by stock",
            mailhtml.stock_rows(report["positions"], report["watch_only"],
                                report["tail"], fmt)
            + (mailhtml.section("The smaller holdings",
                                mailhtml.small_holdings(report["tail"], fmt))
               if (report["tail"] or {}).get("rows") else "")
            + (mailhtml.note("No price for " + ", ".join(report["unpriced"])
                             + " — fix the symbol in Portfolio.")
               if report["unpriced"] else "")))

    fired = alerts_today_lines()
    if fired:
        parts.append("🔔 Your alerts that fired today\n" + "\n".join(fired))
        html_blocks.append(mailhtml.section(
            "Your alerts that fired today",
            mailhtml.bullets([ln.lstrip("• ") for ln in fired])))
        tg_parts.append(tg.b("Alerts today") + "\n"
                        + tg.bullets([ln.lstrip("• ") for ln in fired]))

    rem = reminder_buckets(today)
    if rem["overdue"]:
        parts.append("⚠️ Past their date — not marked done\n"
                     + "\n".join(render_overdue(rem["overdue"], today)))
        tg_parts.append(
            tg.b("⚠️ Past their date") + "\n"
            + "\n".join(f"· {tg.esc(i['text'][:110])}\n  <i>was due "
                        f"{tg.esc(clock.when(i['date'], today))}</i>"
                        for i in rem["overdue"][:_OVERDUE_SHOWN]))
        tg_keys += [(f"✓ Done: {i['text'][:32]}", done_link(i))
                    for i in rem["overdue"][:_OVERDUE_SHOWN]]
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
        tg_parts.append(tg.b(f"📅 Today ({clock.short(today)})") + "\n"
                        + tg.bullets([i["text"][:110] for i in rem["today"]]))
        tg_keys += [(f"✓ Done: {i['text'][:32]}", done_link(i))
                    for i in rem["today"]]
    if rem["upcoming"]:
        parts.append("📆 Coming up\n" + "\n".join(render_upcoming(rem["upcoming"], today)))
        html_blocks.append(mailhtml.section(
            "Coming up", mailhtml.dated_items(_dated(rem["upcoming"], today))))
        tg_parts.append(tg.collapsed(
            f"📆 Coming up ({len(rem['upcoming'])})",
            "\n".join(f"{tg.esc(clock.when(i['date'], today))} — "
                      f"{tg.esc(i['text'][:110])}" for i in rem["upcoming"])))

    rev = review_buckets(today)
    if rev["overdue"] or rev["due"]:
        parts.append("⏰ Advice ledger — calls to revisit\n"
                     + "\n".join(rev["overdue"] + rev["due"]))
        html_blocks.append(mailhtml.section(
            "Advice ledger — calls to revisit",
            mailhtml.bullets([ln.lstrip("• ")
                              for ln in rev["overdue"] + rev["due"]])))
        tg_parts.append(tg.b("⏰ Calls to revisit") + "\n"
                        + tg.bullets([ln.lstrip("• ")
                                      for ln in rev["overdue"] + rev["due"]]))

    for t in worth_knowing(report, today):
        parts.append("💡 Worth knowing\n" + insights.as_text(t)
                     + (f"\n{t['action']}" if t.get("action") else ""))
        html_blocks.append(mailhtml.section(
            "Worth knowing",
            mailhtml.dated_items([{"text": t["text"],
                                   "when": " ".join(x for x in (t.get("why"),
                                                                t.get("action")) if x)}])))
        tg_parts.append(f"{tg.b('💡 Worth knowing')}\n{tg.esc(t['text'])}\n"
                        f"<i>{tg.esc(t.get('why', ''))}</i>")

    n_rules = len(db.get_rules(active_only=True))
    health = "Watcher is healthy" if report["unavailable"] == 0 else \
        f"⚠️ {_plural(report['unavailable'], 'stock')} had no price this run"
    footer = (f"{health} · {_plural(n_rules, 'alert rule')} armed · "
              f"{len(fired)} fired today.")
    tail_note = "Silence from the alert checker means nothing crossed your lines."
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
    tg_parts.append(f"<i>{tg.esc(footer)}</i>")
    app_url = str(settings.get("app_url") or "").rstrip("/")
    if app_url:
        tg_keys.append((f"📱 Open {brand.NAME}", app_url))
    channels = alerts.dispatch(subject, "\n\n".join(parts), html_body=html_body,
                               tg_text=tg.clip("\n\n".join(tg_parts)),
                               tg_buttons=tg.buttons(tg_keys))
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
    tg_parts = [f"{tg.b(brand.NAME + ' · ' + clock.short(today))}\n"
                f"<i>{tg.esc(head.split(' — ')[-1])}</i>"]
    tg_keys: list[tuple[str, str]] = []

    if todo:
        sections.append("⚡ Do today\n" + "\n".join(
            f"{n}. {t}" for n, t in enumerate(todo, 1)))
        html_blocks.append(mailhtml.section("Do today", mailhtml.todo_cards(todo),
                                           "act"))
        tg_parts.append(tg.b("⚡ Do today") + "\n" + "\n".join(
            f"{n}. {tg.esc(t)}" for n, t in enumerate(todo, 1)))
    if rem["overdue"]:
        sections.append("⚠️ Past their date — deal with them or clear them\n"
                        + "\n".join(render_overdue(rem["overdue"], today)))
        html_blocks.append(mailhtml.section(
            "Past their date — deal with them or clear them",
            mailhtml.dated_items(_dated(rem["overdue"][:_OVERDUE_SHOWN], today,
                                        actions=True), "warn"), "warn"))
        tg_parts.append(
            tg.b("⚠️ Past their date") + "\n"
            + "\n".join(f"· {tg.esc(i['text'][:110])}\n  <i>was due "
                        f"{tg.esc(clock.when(i['date'], today))}</i>"
                        for i in rem["overdue"][:_OVERDUE_SHOWN]))
        tg_keys += [(f"✓ Done: {i['text'][:32]}", done_link(i))
                    for i in rem["overdue"][:_OVERDUE_SHOWN]]
    if rev["overdue"]:
        sections.append("⏰ Advice reviews you're late on\n" + "\n".join(rev["overdue"]))
        html_blocks.append(mailhtml.section(
            "Advice reviews you're late on",
            mailhtml.bullets([ln.lstrip("• ") for ln in rev["overdue"]]), "warn"))
        tg_parts.append(tg.b("⏰ Reviews you're late on") + "\n"
                        + tg.bullets([ln.lstrip("• ") for ln in rev["overdue"]]))
    keep = [r for r in ipos["rows"] if r["verdict"] not in ("SKIP", "CLOSED")]
    if not keep and ipos["skip"]:
        # nothing clears the bar: that's one line, not a section with a header
        # over a list of things you're not buying
        line = f"🎯 {ipos['skip'].replace(' others below the bar.', ' open IPOs, none clear the bar.')}"
        # ...but an issue closing TODAY isn't decided by its midday book — SME
        # subscriptions multiply after 2pm, so the verdict that counts comes
        # from the 3:15 last-call alert, and the brief should say so.
        if ipos.get("lastday_pending"):
            n = ipos["lastday_pending"]
            line += (f" {n} of them close today — final numbers come in the "
                     f"3:15 pm last call.")
        sections.append(line)
        html_blocks.append(mailhtml.note(line))
        tg_parts.append(f"<i>{tg.esc(line)}</i>")
    elif keep:
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
        html_blocks.append(mailhtml.section(
            "IPOs open right now",
            mailhtml.ipo_rows(keep)
            + (mailhtml.note(ipos["skip"]) if ipos["skip"] else "")))
        # as columns: premium, book, QIB and when it ends — the four numbers the
        # house rules are actually checked against
        tg_parts.append(
            tg.b("🎯 IPOs open") + "\n"
            + tg.table(["IPO", "GMP", "Book", "QIB", "Ends"],
                       [[tg.short_symbol(r["name"]),
                         (f"{r['gmp_pct']:g}%" if r.get("gmp_pct") is not None
                          else "—"),
                         (f"{r['total']:g}x" if r.get("total") is not None
                          else "—"),
                         (f"{r['qib']:g}x" if r.get("qib") is not None else "—"),
                         r.get("ends", "?")] for r in keep])
            + (f"\n<i>{tg.esc(ipos['skip'])}</i>" if ipos["skip"] else ""))
    if drops:
        sections.append("🛒 Tracked prices moved\n" + "\n".join(drops))
        html_blocks.append(mailhtml.section("Tracked prices moved",
                                           mailhtml.bullets(drops)))
        tg_parts.append(tg.b("🛒 Prices moved") + "\n" + tg.bullets(drops))
    if rem["upcoming"]:
        sections.append("📆 Coming up (nothing to do yet)\n"
                        + "\n".join(render_upcoming(rem["upcoming"], today)))
        html_blocks.append(mailhtml.section(
            "Coming up (nothing to do yet)",
            mailhtml.dated_items(_dated(rem["upcoming"], today))))
        tg_parts.append(tg.collapsed(
            f"📆 Coming up ({len(rem['upcoming'])})",
            "\n".join(f"{tg.esc(clock.when(i['date'], today))} — "
                      f"{tg.esc(i['text'][:110])}" for i in rem["upcoming"])))
    if rev["due"]:
        sections.append("⏰ Advice reviews due\n" + "\n".join(rev["due"]))
        html_blocks.append(mailhtml.section(
            "Advice reviews due",
            mailhtml.bullets([ln.lstrip("• ") for ln in rev["due"]])))
        tg_parts.append(tg.b("⏰ Reviews due") + "\n"
                        + tg.bullets([ln.lstrip("• ") for ln in rev["due"]]))

    if not sections:
        print("[heartbeat] morning brief: nothing due, no open IPOs — staying quiet")
        return []
    count = len(todo)
    subject = f"{brand.BRIEF_SUBJECT} · {clock.short(today)} · " + (
        f"{count} thing{'s' if count > 1 else ''} to do today" if count
        else "midday brief")
    html_body = mailhtml.page(brand.BRIEF_TITLE, head, html_blocks,
                              ipos.get("footer", ""))
    if ipos.get("footer"):
        tg_parts.append(f"<i>{tg.esc(ipos['footer'])}</i>")
    app_url = str(settings.get("app_url") or "").rstrip("/")
    if app_url:
        tg_keys.append((f"📱 Open {brand.NAME}", app_url))
    channels = alerts.dispatch(subject, "\n\n".join(body + sections),
                               html_body=html_body,
                               tg_text=tg.clip("\n\n".join(tg_parts)),
                               tg_buttons=tg.buttons(tg_keys))
    db.log_alert(None, "DIGEST", "-", "midday brief", channels)
    print(f"[heartbeat] morning brief sent to {channels or 'no channel configured'}")
    return channels


_BAR_WORDS = {"gmp_pct": "premium", "total": "book", "qib": "QIB"}


def send_ipo_lastcall() -> list[str]:
    """The 15:15 IST last call on IPOs closing today.

    Exists because the bars are calibrated on closing books and the midday
    brief can only see midday books — SME subscriptions multiply in the final
    hours. Sends nothing at all unless there's a decision to make: every bar
    passed (apply now), or exactly one bar within a fifth of its line (your
    judgement, laid out). Silence means today's closers stayed below the bar.
    """
    try:
        lc = ipo.last_call()
    except Exception as e:
        print(f"[heartbeat] last call skipped, screener errored: {str(e)[:100]}")
        return []
    if not lc["apply"] and not lc["close"]:
        print("[heartbeat] last call: nothing passing or close, staying silent")
        return []

    lines, tg_parts, html_blocks = [], [], []
    for r in lc["apply"]:
        kind = "SME" if r.get("sme") else "mainboard"
        line = (f"{r['name']} ({kind}) — {ipo.numbers_phrase(r, compact=True)}. "
                f"Every bar passed. Apply before 4 pm, one lot, one PAN.")
        lines.append("🚨 " + line)
        tg_parts.append(f"🚨 {tg.b(tg.esc(r['name']))} — "
                        f"{tg.esc(ipo.numbers_phrase(r, compact=True))}\n"
                        f"every bar passed · <b>apply before 4 pm</b>, 1 lot")
        html_blocks.append(mailhtml.section(
            f"Apply: {r['name']}",
            mailhtml.bullets([ipo.numbers_phrase(r),
                              "Every bar passed — one lot, one PAN, before 4 pm."]),
            "act"))
    for r in lc["close"]:
        k, v, need = r["near"]
        word = _BAR_WORDS.get(k, k)
        fmt = (lambda x: f"{x:g}%") if k == "gmp_pct" else (lambda x: f"{x:g}x")
        line = (f"{r['name']} — {ipo.numbers_phrase(r, compact=True)}. "
                f"Only the {word} misses: {fmt(v)} against {fmt(need)}. "
                f"Books can still fill by 4 pm — your call.")
        lines.append("🤏 " + line)
        tg_parts.append(f"🤏 {tg.b(tg.esc(r['name']))} — "
                        f"{tg.esc(ipo.numbers_phrase(r, compact=True))}\n"
                        f"only the {word} misses ({tg.esc(fmt(v))} vs "
                        f"{tg.esc(fmt(need))}) · <i>your call, 4 pm</i>")
        html_blocks.append(mailhtml.section(
            f"Close: {r['name']}",
            mailhtml.bullets([ipo.numbers_phrase(r),
                              f"Only the {word} misses — {fmt(v)} against "
                              f"{fmt(need)}. Books can still fill by 4 pm."]),
            "warn"))

    stamp = clock.clock_time()
    body = (f"IPO last call · {stamp} IST\n\n" + "\n".join(lines)
            + (f"\n\n{lc['footer']}" if lc["footer"] else ""))
    subject = f"🎯 {brand.NAME}: IPO last call — closes 4 pm today"
    html_body = mailhtml.page(
        "IPO last call", f"{stamp} IST — bids close at 4 pm",
        html_blocks, lc["footer"])
    tg_text = tg.clip(f"{tg.b('🎯 IPO last call')} · {tg.esc(stamp)} IST\n\n"
                      + "\n\n".join(tg_parts))
    channels = alerts.dispatch(subject, body, html_body=html_body,
                               tg_text=tg_text)
    db.log_alert(None, "DIGEST", "-", "ipo last call", channels)
    print(f"[heartbeat] last call sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    if "morning" in sys.argv[1:]:
        send_morning()
    elif "lastcall" in sys.argv[1:]:
        send_ipo_lastcall()
    else:
        send_daily()
