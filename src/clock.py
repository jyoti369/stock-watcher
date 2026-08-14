"""India-time clock and date wording.

Two reasons this exists. One, the scheduled jobs run on GitHub's machines,
which are on UTC — so "today" has to be resolved in India or a late-evening run
would compare reminders against yesterday. Two, every date that reaches a mail
should say which day it means. "Due:" over a line dated tomorrow reads as
today; "Fri 14 Aug (tomorrow)" can't be misread.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_today() -> date:
    return ist_now().date()


def to_ist(iso: str) -> datetime | None:
    """Parse a stored UTC timestamp into IST. None if unparseable."""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def clock_time(when: datetime | None = None) -> str:
    """'3:45 pm' — no leading zero, lowercase meridiem."""
    when = when or ist_now()
    return when.strftime("%I:%M %p").lstrip("0").lower()


def stamp(when: datetime | None = None) -> str:
    """'Friday, 14 August 2026 · 3:45 pm IST' — the header line of every mail."""
    when = when or ist_now()
    return (f"{when.strftime('%A')}, {when.day} {when.strftime('%B %Y')} · "
            f"{clock_time(when)} IST")


def short(d: date) -> str:
    """'Fri 14 Aug'."""
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def gap_phrase(d: date, today: date | None = None) -> str:
    """How far off a date is, in words: 'today', 'tomorrow', 'in 3 days',
    'yesterday', '2 days ago'."""
    today = today or ist_today()
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def when(d: date, today: date | None = None) -> str:
    """'Fri 14 Aug (tomorrow)' — the date and its distance, never one alone."""
    return f"{short(d)} ({gap_phrase(d, today)})"
