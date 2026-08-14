"""App preferences, kept in the repo so the phone and the laptop agree.

Nothing here is private (it's which panels you want to see), so unlike
holdings this file is committed as plain JSON. Everything has a default, and a
missing or corrupt file falls back to those defaults rather than erroring —
a settings file should never be able to break the app.
"""
from __future__ import annotations

import json

from .repo_state import STATE_DIR

SETTINGS_JSON = STATE_DIR / "settings.json"

DEFAULTS: dict = {
    # the smart banner above the tabs
    "banner": True,
    "banner_tips": 2,               # how many tips to show at once (1-3)
    "banner_categories": ["risk", "money", "tax", "hygiene", "habit"],
    "banner_min_urgency": 0,        # 60 = only show things that look urgent
    # put the top tip into the daily digest mail too
    "digest_tips": True,
    # the long grey "how to read this" captions — helpful once, noise later
    "explainers": True,
    # how the digest orders your stocks, and how many get a full row before the
    # rest are listed compactly
    "sort_by": "value",             # value | pnl | pnl_pct | day
    "positions_shown": 12,          # 0 = give every holding its own row
    # base URL of the hosted app, used for the "mark done" links in the mails
    "app_url": "https://stock-watcher-zddsancprsyurdcql5zqis.streamlit.app",
    "mail_actions": True,           # put those tap-to-act links in the mails
}

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


def save(values: dict) -> bool:
    """Write only the keys we know about, so a typo can't accumulate junk."""
    current = load()
    merged = {k: values.get(k, current.get(k, v)) for k, v in DEFAULTS.items()}
    if merged == current and SETTINGS_JSON.exists():
        return True                          # no write, no git churn
    STATE_DIR.mkdir(exist_ok=True)
    SETTINGS_JSON.write_text(json.dumps(merged, indent=2, sort_keys=True))
    return True
