"""Tracked buys — the app's own price history, one snapshot per day.

Free price-history charts only exist for Amazon (Keepa renders one per ASIN).
Flipkart and Myntra keep theirs behind JS apps, so for anything worth waiting
on we simply build our own: 📌 Track an item in the Buy tab and the midday
heartbeat re-reads its live price every weekday, appends a {date, price}
point, and pings Telegram when it moves down meaningfully (>=3%) or hits your
target. The chart in the tab grows out of those points — sparse at first,
honest forever.

Item shape: {title, url, source, added, target?, history: [{d, p}]}.
Encrypted at rest like reminders; no state key -> nothing is ever written.
"""
from __future__ import annotations

import json
import time
from datetime import date

from .repo_state import STATE_DIR, _fernet

WATCH_JSON = STATE_DIR / "shop_watch.json"


def load() -> list[dict] | None:
    f = _fernet()
    if f is None:
        return None
    try:
        raw = json.loads(WATCH_JSON.read_text())
        return json.loads(f.decrypt(raw["cipher"].encode()).decode())
    except Exception:
        return []


def save(rows: list[dict]) -> bool:
    f = _fernet()
    if f is None:
        return False
    STATE_DIR.mkdir(exist_ok=True)
    WATCH_JSON.write_text(json.dumps(
        {"encrypted": True,
         "cipher": f.encrypt(json.dumps(rows, ensure_ascii=False).encode()).decode()}))
    return True


def add(title: str, url: str, source: str, price: float | None,
        target: float | None = None) -> bool:
    """Start tracking; the price seen at add time is point one."""
    rows = load()
    if rows is None:
        return False
    if any(r["url"] == url for r in rows):
        return True                                   # already tracked
    item = {"title": title[:90], "url": url, "source": source,
            "added": date.today().isoformat(), "history": []}
    if price:
        item["history"].append({"d": date.today().isoformat(), "p": price})
    if target:
        item["target"] = target
    rows.append(item)
    return save(rows)


def remove(url: str) -> bool:
    rows = load()
    if rows is None:
        return False
    return save([r for r in rows if r["url"] != url])


def record(item: dict, price: float, today: str) -> None:
    """One point per day; a same-day re-check just refreshes the value."""
    hist = item.setdefault("history", [])
    if hist and hist[-1]["d"] == today:
        hist[-1]["p"] = price
    else:
        hist.append({"d": today, "p": price})
    del hist[:-180]                                   # ~6 months is plenty


def check_all(fetch=None, today: str | None = None) -> tuple[list[dict], list[str]]:
    """Re-price every tracked item, persist the new points, and return
    (items, alert_lines). `fetch` is injectable for tests; defaults to the
    live per-store price reader in shop.py."""
    if fetch is None:
        from .shop import current_price as fetch
    rows = load() or []
    today = today or date.today().isoformat()
    alerts = []
    for item in rows:
        prev = item["history"][-1]["p"] if item.get("history") else None
        price = fetch(item["url"], item["source"])
        if price is None:
            continue
        record(item, price, today)
        low = min(pt["p"] for pt in item["history"])
        if item.get("target") and price <= item["target"]:
            alerts.append(f"🎯 ₹{price:g} hits your ₹{item['target']:g} target"
                          f" — {item['title'][:60]}\n{item['url']}")
        elif prev and price <= prev * 0.97:
            alerts.append(f"📉 ₹{prev:g} → ₹{price:g}"
                          + (" (new low)" if price <= low else "")
                          + f" — {item['title'][:60]}\n{item['url']}")
        time.sleep(1)                                 # gentle on the stores
    if rows:
        save(rows)
    return rows, alerts


def digest_lines() -> list[str]:
    """Alert lines for the midday brief; [] when nothing moved."""
    _, alert_lines = check_all()
    return alert_lines
