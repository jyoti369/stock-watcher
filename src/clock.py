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


# NSE/BSE equity hours, IST. Pre-open is the 09:00-09:15 auction.
PRE_OPEN = (9, 0)
OPEN = (9, 15)
CLOSE = (15, 30)
# The house rule for IPO applications: bids in before 4pm on the last day.
IPO_CUTOFF = (16, 0)


def _hm(when_: datetime) -> tuple[int, int]:
    return (when_.hour, when_.minute)


def market_status(now: datetime | None = None) -> dict:
    """Where the trading day is right now: {phase, open, label}.

    phase is one of pre-open / open / closed / weekend. `open` means orders are
    actually being matched, which is what decides whether a price on screen is
    live or the last trade of a previous session.

    Exchange holidays are NOT known here — NSE's calendar endpoint refuses
    datacenter IPs and 403s even from home, so a Monday holiday will read as
    "open". The label says "market hours" rather than "trading" for that reason;
    anything that would mislead should show the price timestamp beside it.
    """
    now = now or ist_now()
    hm = _hm(now)
    if now.weekday() >= 5:                      # Saturday, Sunday
        return {"phase": "weekend", "open": False,
                "label": f"Market shut for the weekend · opens "
                         f"{'Mon' if now.weekday() == 5 else 'tomorrow'} 9:15 am"}
    if hm < PRE_OPEN:
        return {"phase": "closed", "open": False,
                "label": "Market opens at 9:15 am"}
    if hm < OPEN:
        return {"phase": "pre-open", "open": False,
                "label": "Pre-open auction · trading starts 9:15 am"}
    if hm < CLOSE:
        return {"phase": "open", "open": True,
                "label": "Market hours · closes 3:30 pm"}
    return {"phase": "closed", "open": False,
            "label": f"Market closed at 3:30 pm · "
                     f"{'Mon' if now.weekday() == 4 else 'tomorrow'} 9:15 am next"}


def market_open(now: datetime | None = None) -> bool:
    return market_status(now)["open"]


def past_ipo_cutoff(now: datetime | None = None) -> bool:
    """True once the 4pm application deadline for the day has gone."""
    return _hm(now or ist_now()) >= IPO_CUTOFF


def price_note(now: datetime | None = None) -> str:
    """How to read a price shown at this moment."""
    st = market_status(now)
    return "live price" if st["open"] else "last traded price, market is closed"
