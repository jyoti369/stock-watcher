"""Buy/sell advice ledger, encrypted at rest like holdings.

Every standing call on a holding gets one entry: the stance, the one-line
reason, the trigger that would change it, and a review-by date. Entries are
never silently deleted — they get closed with an outcome (right/wrong/moot),
so the ledger doubles as an honesty scoreboard for past calls.
"""
from __future__ import annotations

import json
from datetime import date

from .repo_state import STATE_DIR, _fernet

ADVICE_JSON = STATE_DIR / "advice.json"

STANCES = ["KEEP", "SELL", "HOLD-RULE", "WATCH"]
STATUSES = ["OPEN", "DONE-RIGHT", "DONE-WRONG", "DONE-MOOT"]


def load_advice() -> list[dict] | None:
    """All entries, or None (missing / no key / wrong key)."""
    f = _fernet()
    if f is None:
        return None
    try:
        raw = json.loads(ADVICE_JSON.read_text())
        return json.loads(f.decrypt(raw["cipher"].encode()).decode())
    except Exception:
        return None


def save_advice(rows: list[dict]) -> bool:
    """Encrypt and write. False (and no write) when no key is set."""
    f = _fernet()
    if f is None:
        return False
    current = load_advice()
    if current is not None and json.dumps(current, sort_keys=True) == \
            json.dumps(rows, sort_keys=True):
        return True
    STATE_DIR.mkdir(exist_ok=True)
    ADVICE_JSON.write_text(json.dumps(
        {"encrypted": True,
         "cipher": f.encrypt(json.dumps(rows, ensure_ascii=False).encode()).decode()}))
    return True


def new_entry(symbol: str, stance: str, thesis: str, trigger: str = "",
              review_by: str = "") -> dict:
    return {"symbol": symbol.upper(), "stance": stance, "thesis": thesis,
            "trigger": trigger, "review_by": review_by,
            "added": date.today().isoformat(), "status": "OPEN", "outcome": ""}
