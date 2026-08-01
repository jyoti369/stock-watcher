"""Daily heartbeat digest.

Once a day (after market close) this sends a short summary of your watchlist plus
a health line. The point is trust: if the watcher ever breaks or the data source
goes dark, you'll see it here — so silence from the alert watcher genuinely means
"nothing triggered", not "it quietly died".
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from . import advice, alerts, analysis, db, reminders, watcher

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


def reminder_due_lines() -> list[str]:
    """Dated reminders arriving within a week (empty without the key)."""
    rows = reminders.load()
    if not rows:
        return []
    today = date.today()
    out = []
    for r in rows:
        if reminders.due(r, today):
            eff = reminders.effective_date(r, today)
            when = advice.pretty_date(eff.isoformat()) if eff else ""
            out.append(f"• {r['text']}" + (f" (by {when})" if when else ""))
    return out


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
    rem = reminder_due_lines()
    if rem:
        body += "\n\n📅 Reminders due:\n" + "\n".join(rem)
    body += f"\n\nWatcher {health} · {n_rules} rule(s) active · {n_fired} alert(s) fired today."
    body += "\n\n(daily heartbeat — if you got this, the watcher is alive; silence from it means nothing triggered)"
    channels = alerts.dispatch("📊 Stock Watcher — daily digest", body)
    db.log_alert(None, "DIGEST", "-", f"daily digest ({unavailable} unavailable)", channels)
    print(f"[heartbeat] sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    send_daily()
