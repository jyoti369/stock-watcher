"""Daily heartbeat digests.

Two sends a day. The after-close one (~3:45pm IST) is the market summary plus
a health line — the point is trust: if the watcher ever breaks or the data
source goes dark, you'll see it here, so silence from the alert watcher
genuinely means "nothing triggered", not "it quietly died".

The midday one (~12:05pm IST) is the actionable brief: reminders and advice
calls due, plus the live IPO screener — timed so there's still the whole
afternoon to actually apply/act before cutoffs. It stays silent on days with
nothing due and no open IPOs.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone

from . import advice, alerts, analysis, db, ipo, reminders, watcher

_BADGE = {"OK": "🟢", "Mixed": "🟡", "Weak": "🔴", "Unknown": "⚪"}


def build_digest() -> tuple[list[str], int, int, int]:
    """Returns (per-stock lines, active_rule_count, alerts_fired_today, unavailable_count)."""
    lines, unavailable = [], 0
    for w in db.get_watchlist():
        v = watcher.gather_values(w["symbol"], w["exchange"])
        price = v.get("price")
        if price is None:
            unavailable += 1
            lines.append(f"{w['symbol']}: data unavailable")
            continue
        day = v.get("pct_change_day")
        rating = analysis.score_fundamentals(w["symbol"], w["exchange"]).get("rating")
        if isinstance(day, (int, float)):
            arrow = "🔺" if day > 0 else "🔻" if day < 0 else "▪️"
            day_txt = f"{arrow} {day:+.1f}%"
        else:
            day_txt = "—"
        # the colour dot is BUSINESS HEALTH (fundamentals), not price direction —
        # label it so a green dot next to a red day doesn't read as "up"
        lines.append(f"{w['symbol']}: ₹{price:,.0f} ({day_txt}) · "
                     f"health {_BADGE.get(rating, '⚪')}")

    today = datetime.now(timezone.utc).date().isoformat()
    fired_today = sum(1 for h in db.get_alert_history(limit=100) if h["ts"][:10] == today)
    return lines, len(db.get_rules(active_only=True)), fired_today, unavailable


def review_due_lines() -> list[str]:
    """Advice-ledger calls whose catalyst/review date has arrived.

    Empty when the encryption key isn't available to this run (e.g. the Action
    without STOCKWATCH_STATE_KEY) — the ledger simply stays silent, never leaks.
    """
    rows = advice.load_advice()
    if not rows:
        return []
    today = date.today()
    verb = {"SELL": "action due — sell", "TRIM": "trim due", "HOLD-RULE": "decision due"}
    out = []
    for a in rows:
        if advice.due_soon(a, today):
            when = advice.pretty_date(a.get("catalyst_date") or a.get("review_by"))
            v = verb.get(a.get("stance"), "re-examine")
            out.append(f"• {a['symbol']}: {v}" + (f" (by {when})" if when else ""))
    return out


def reminder_due_lines() -> tuple[list[str], list[str]]:
    """Dated reminders as (due today or overdue, coming up within a week).

    Kept separate because a line like "X IPO last day" sitting under a plain
    "Due:" header reads as TODAY even when its date is tomorrow. Both lists
    are empty without the key.
    """
    rows = reminders.load()
    if not rows:
        return [], []
    today = date.today()
    now, later = [], []
    for r in rows:
        if not reminders.due(r, today):
            continue
        eff = reminders.effective_date(r, today)
        when = advice.pretty_date(eff.isoformat()) if eff else ""
        if eff and eff > today:
            gap = (eff - today).days
            later.append(f"• {when} ({'tomorrow' if gap == 1 else f'in {gap} days'}): "
                         f"{r['text']}")
        elif eff and eff < today:
            now.append(f"• {r['text']} (was due {when} — overdue)")
        else:
            now.append(f"• {r['text']}")
    return now, later


def send_daily() -> list[str]:
    if not db.get_watchlist():
        print("[heartbeat] watchlist empty, nothing to send")
        return []
    lines, n_rules, n_fired, unavailable = build_digest()
    health = "healthy" if unavailable == 0 else f"⚠️ {unavailable} stock(s) had no data this run"
    body = "\n".join(lines)
    reviews = review_due_lines()
    if reviews:
        body += "\n\n⏰ Advice review due:\n" + "\n".join(reviews)
    due_now, upcoming = reminder_due_lines()
    if due_now:
        body += "\n\n📅 Reminders due today:\n" + "\n".join(due_now)
    if upcoming:
        body += "\n\n📆 Coming up (not due yet):\n" + "\n".join(upcoming)
    body += f"\n\nWatcher {health} · {n_rules} rule(s) active · {n_fired} alert(s) fired today."
    body += "\n\n(daily heartbeat — if you got this, the watcher is alive; silence from it means nothing triggered)"
    channels = alerts.dispatch("📊 Stock Watcher — daily digest", body)
    db.log_alert(None, "DIGEST", "-", f"daily digest ({unavailable} unavailable)", channels)
    print(f"[heartbeat] sent to {channels or 'no channel configured'}")
    return channels


def send_morning() -> list[str]:
    """Midday action brief: due reminders/advice + IPO screener. Skips the
    send entirely when there's nothing to act on — no noise, only signal."""
    parts = []
    due_now, upcoming = reminder_due_lines()
    if due_now:
        parts.append("📅 Due TODAY:\n" + "\n".join(due_now))
    if upcoming:
        parts.append("📆 Coming up (not due yet):\n" + "\n".join(upcoming))
    reviews = review_due_lines()
    if reviews:
        parts.append("⏰ Advice review due:\n" + "\n".join(reviews))
    try:
        ipos = ipo.digest_lines()
    except Exception as e:                      # a broken scrape shouldn't kill the brief
        ipos = [f"(IPO screener errored: {str(e)[:80]})"]
    if ipos:
        parts.append("🎯 IPO screener (house rules: MB 20%/15x/QIB5x · "
                     "SME 35%/25x/QIB2x, last-day apply, 1 lot):\n"
                     + "\n".join("• " + ln for ln in ipos))
    try:
        from . import shop_watch
        drops = shop_watch.digest_lines()
    except Exception as e:                      # store blocks shouldn't kill the brief
        drops = [f"(price tracker errored: {str(e)[:80]})"]
    if drops:
        parts.append("🛒 Tracked prices moved:\n" + "\n".join(drops))
    if not parts:
        print("[heartbeat] morning brief: nothing due, no open IPOs — staying quiet")
        return []
    channels = alerts.dispatch("🌅 Stock Watcher — midday brief", "\n\n".join(parts))
    db.log_alert(None, "DIGEST", "-", "midday brief", channels)
    print(f"[heartbeat] morning brief sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    send_morning() if "morning" in sys.argv[1:] else send_daily()
