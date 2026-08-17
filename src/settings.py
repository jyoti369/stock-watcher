"""App preferences, kept in the repo so the phone and the laptop agree.

Nothing here is private (it's which panels you want to see), so unlike
holdings this file is committed as plain JSON. Everything has a default, and a
missing or corrupt file falls back to those defaults rather than erroring —
a settings file should never be able to break the app.
"""
from __future__ import annotations

import json

from . import clock
from .repo_state import STATE_DIR

SETTINGS_JSON = STATE_DIR / "settings.json"

DEFAULTS: dict = {
    # the smart banner above the tabs
    "banner": True,
    "banner_tips": 1,               # how many tips to show at once (1-3)
    "banner_categories": ["risk", "money", "tax", "hygiene", "habit"],
    "banner_min_urgency": 0,        # 60 = only show things that look urgent
    # put the top tip into the daily digest mail too
    "digest_tips": True,
    # the long grey "how to read this" captions. Off by default: they were
    # written as help and became furniture — "the more info the more confusion".
    "explainers": False,
    # how the digest orders your stocks, and how many get a full row before the
    # rest are listed compactly
    "sort_by": "value",             # value | pnl | pnl_pct | day
    "positions_shown": 12,          # 0 = give every holding its own row
    # base URL of the hosted app, used for the "mark done" links in the mails
    "app_url": "https://stock-watcher-zddsancprsyurdcql5zqis.streamlit.app",
    "mail_actions": True,           # put those tap-to-act links in the mails
    # The day you last told the app your holdings are correct. Nothing else can
    # know this: no broker feed is wired up, so a stock you sold keeps showing
    # until you say otherwise. The DB's own added_at is no use — it records when
    # the row was rebuilt from committed state, which happens on every deploy.
    "holdings_as_of": "",
    # The IPO bars, editable because they're a judgement call, not a fact. The
    # goal they serve: if you're allotted, don't lose money on listing day. So
    # the premium is a cushion against a weak open, and QIB is the check that
    # the book is real rather than painted. Separate sets because SME premiums
    # move on much thinner books than mainboard ones.
    "ipo_rules": {
        "mainboard": {"gmp_pct": 15.0, "total": 10.0, "qib": 5.0},
        "sme": {"gmp_pct": 30.0, "total": 20.0, "qib": 2.0},
    },
}

# what a rule set is allowed to contain, and the sane range for each, so a
# fat-fingered 300% bar can't silently reject every IPO forever
IPO_BARS = {"gmp_pct": (0.0, 100.0), "total": (0.0, 200.0), "qib": (0.0, 100.0)}

SORTS = {"value": "biggest holding first",
         "pnl": "biggest profit or loss in rupees",
         "pnl_pct": "biggest gain or loss in percent",
         "day": "today's movers first"}


def load() -> dict:
    try:
        saved = json.loads(SETTINGS_JSON.read_text())
        if not isinstance(saved, dict):
            raise ValueError
    except Exception:
        saved = {}
    return {**DEFAULTS, **saved}


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def ipo_rules() -> dict:
    """The two rule sets, always complete and always in range.

    Read defensively rather than trusting the file: a half-written segment, a
    string where a number belongs, or a bar someone typed as 3000 all fall back
    to the default for that one bar instead of breaking the screener. A rule
    that silently rejects every IPO is worse than a rule that's too loose,
    because nothing on screen tells you it happened.
    """
    saved = load().get("ipo_rules") or {}
    out = {}
    for kind, base in DEFAULTS["ipo_rules"].items():
        got = saved.get(kind) if isinstance(saved, dict) else None
        got = got if isinstance(got, dict) else {}
        merged = {}
        for bar, default in base.items():
            lo, hi = IPO_BARS[bar]
            try:
                val = float(got[bar])
            except (KeyError, TypeError, ValueError):
                val = default
            merged[bar] = default if not lo <= val <= hi else val
        out[kind] = merged
    return out


def touch_holdings() -> None:
    """Record that the holdings list is correct as of today. Called whenever you
    import, add, remove or confirm one."""
    save({**load(), "holdings_as_of": clock.ist_today().isoformat()})


def holdings_age(today=None) -> int | None:
    """Days since the holdings were last confirmed, or None if never."""
    raw = load().get("holdings_as_of")
    if not raw:
        return None
    try:
        from datetime import date
        return ((today or clock.ist_today()) - date.fromisoformat(str(raw)[:10])).days
    except (ValueError, TypeError):
        return None


def save(values: dict) -> bool:
    """Write only the keys we know about, so a typo can't accumulate junk."""
    current = load()
    merged = {k: values.get(k, current.get(k, v)) for k, v in DEFAULTS.items()}
    if merged == current and SETTINGS_JSON.exists():
        return True                          # no write, no git churn
    STATE_DIR.mkdir(exist_ok=True)
    SETTINGS_JSON.write_text(json.dumps(merged, indent=2, sort_keys=True))
    return True
