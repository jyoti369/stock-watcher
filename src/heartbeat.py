"""Daily heartbeat digest.

Once a day (after market close) this sends a short summary of your watchlist plus
a health line. The point is trust: if the watcher ever breaks or the data source
goes dark, you'll see it here — so silence from the alert watcher genuinely means
"nothing triggered", not "it quietly died".
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import alerts, analysis, db, watcher

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
        day_txt = f"{day:+.1f}%" if isinstance(day, (int, float)) else "—"
        lines.append(f"{w['symbol']}: ₹{price:,.0f} ({day_txt}) {_BADGE.get(rating, '⚪')}")

    today = datetime.now(timezone.utc).date().isoformat()
    fired_today = sum(1 for h in db.get_alert_history(limit=100) if h["ts"][:10] == today)
    return lines, len(db.get_rules(active_only=True)), fired_today, unavailable


def send_daily() -> list[str]:
    if not db.get_watchlist():
        print("[heartbeat] watchlist empty, nothing to send")
        return []
    lines, n_rules, n_fired, unavailable = build_digest()
    health = "healthy" if unavailable == 0 else f"⚠️ {unavailable} stock(s) had no data this run"
    body = "\n".join(lines)
    body += f"\n\nWatcher {health} · {n_rules} rule(s) active · {n_fired} alert(s) fired today."
    body += "\n\n(daily heartbeat — if you got this, the watcher is alive; silence from it means nothing triggered)"
    channels = alerts.dispatch("📊 Stock Watcher — daily digest", body)
    db.log_alert(None, "DIGEST", "-", f"daily digest ({unavailable} unavailable)", channels)
    print(f"[heartbeat] sent to {channels or 'no channel configured'}")
    return channels


if __name__ == "__main__":
    send_daily()
