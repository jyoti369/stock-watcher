"""Dated reminders, encrypted at rest like the rest of the private state.

A reminder is {text, date (ISO), yearly, monthly, until, done}. One-offs fire
once on their date (and show as overdue after). Yearly ones (e.g. the April
PPF top-up) use only the month/day, so they roll forward on their own every
year — no editing. Monthly ones roll forward every month on the same day, and
an optional `until` date makes them stop by themselves (e.g. a 10-month
redeem-and-reinvest routine that shouldn't nag forever).

Like the advice ledger, due reminders surface in the app AND get pushed into
the daily Telegram digest, so a date you set months ago still nudges you when
it arrives. Needs the state key; with no key nothing is ever written.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from . import clock
from .repo_state import STATE_DIR, _fernet

REMINDERS_JSON = STATE_DIR / "reminders.json"


def ref(r: dict) -> str:
    """Short stable id for one reminder, derived from its own content.

    Used by the tap-to-mark-done links in the mails: a row index would break
    the moment a reminder is added or removed, and the text itself can't go in a
    URL. Eight hex characters over text+date is plenty for a personal list, and
    it changes if the reminder is edited — which is correct, since an edited
    reminder is a different job.
    """
    raw = f"{r.get('text', '')}|{str(r.get('date', ''))[:10]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def find(rows: list[dict], reference: str) -> dict | None:
    return next((r for r in rows or [] if ref(r) == reference), None)


def mark_done(reference: str) -> str | None:
    """Tick off the reminder with this id. Returns its text, or None if it
    wasn't found (already done and edited, or a stale link)."""
    rows = load()
    if rows is None:
        return None
    target = find(rows, reference)
    if target is None:
        return None
    # a repeating reminder shouldn't be killed by one tick — it rolls forward on
    # its own, so record the date it was last handled instead
    if repeats(target):
        target["last_done"] = clock.ist_today().isoformat()
    else:
        target["done"] = True
    save(rows)
    return target.get("text")


def load() -> list[dict] | None:
    f = _fernet()
    if f is None:
        return None
    try:
        raw = json.loads(REMINDERS_JSON.read_text())
        return json.loads(f.decrypt(raw["cipher"].encode()).decode())
    except Exception:
        return None


def save(rows: list[dict]) -> bool:
    f = _fernet()
    if f is None:
        return False
    current = load()
    if current is not None and json.dumps(current, sort_keys=True) == \
            json.dumps(rows, sort_keys=True):
        return True
    STATE_DIR.mkdir(exist_ok=True)
    REMINDERS_JSON.write_text(json.dumps(
        {"encrypted": True,
         "cipher": f.encrypt(json.dumps(rows, ensure_ascii=False).encode()).decode()}))
    return True


def new(text: str, on: str, yearly: bool = False, monthly: bool = False,
        until: str | None = None) -> dict:
    r = {"text": text, "date": on, "yearly": bool(yearly),
         "monthly": bool(monthly), "done": False,
         "created": clock.ist_today().isoformat()}
    if until:
        r["until"] = until
    return r


def repeats(r: dict) -> bool:
    return bool(r.get("yearly") or r.get("monthly"))


def _safe_date(y: int, m: int, d: int) -> date:
    while d > 28:
        try:
            return date(y, m, d)
        except ValueError:
            d -= 1
    return date(y, m, d)


def effective_date(r: dict, today: date) -> date | None:
    """When this reminder next fires. For yearly, the upcoming month/day
    occurrence (this year if still ahead, else next year); for monthly, the
    upcoming same-day-of-month occurrence, or None once past `until`.
    None if unparseable."""
    try:
        base = date.fromisoformat(str(r["date"])[:10])
    except (ValueError, KeyError, TypeError):
        return None
    if r.get("monthly"):
        if base >= today:
            cand = base
        else:
            cand = _safe_date(today.year, today.month, base.day)
            if cand < today:
                y, m = (today.year + 1, 1) if today.month == 12 else \
                    (today.year, today.month + 1)
                cand = _safe_date(y, m, base.day)
        until = r.get("until")
        if until and cand > date.fromisoformat(str(until)[:10]):
            return None
        return cand
    if not r.get("yearly"):
        return base
    cand = _safe_date(today.year, base.month, base.day)
    if cand < today:
        cand = _safe_date(today.year + 1, base.month, base.day)
    return cand


def due(r: dict, today: date, horizon_days: int = 7) -> bool:
    if r.get("done") and not repeats(r):
        return False
    eff = effective_date(r, today)
    if eff is None:
        return False
    # a repeating reminder ticked off for this occurrence goes quiet until the
    # next one comes round, instead of nagging for the rest of the day
    if r.get("last_done") and str(r["last_done"])[:10] >= eff.isoformat():
        return False
    return eff <= today + timedelta(days=horizon_days)
