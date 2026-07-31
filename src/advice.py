"""Buy/sell advice ledger, encrypted at rest like holdings.

Each holding gets one standing call: a stance, the one-line reason, a *catalyst*
(what to watch for and roughly when), optional exit bands (sell-above /
stop-below), and a review-by date. Two things make it an advisor rather than a
note:

  * exit bands turn straight into live watcher alerts (`alert_rules_from`), so
    every stock's sell/stop line is armed, not just remembered;
  * `due_soon` surfaces calls whose catalyst or review date has arrived, so the
    ledger nudges you instead of waiting to be re-read.

Calls are never silently deleted — they close with an outcome
(right/wrong/moot), so the ledger doubles as an honesty scoreboard.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .repo_state import STATE_DIR, _fernet

ADVICE_JSON = STATE_DIR / "advice.json"

STANCES = ["KEEP", "TRIM", "SELL", "HOLD-RULE", "WATCH"]
STATUSES = ["OPEN", "DONE-RIGHT", "DONE-WRONG", "DONE-MOOT"]
_RULE_TAG = "(advice)"                     # marks watcher rules this module owns


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


def new_entry(symbol: str, stance: str, thesis: str, catalyst: str = "",
              catalyst_date: str = "", sell_above: float | None = None,
              stop_below: float | None = None, review_by: str = "") -> dict:
    return {"symbol": symbol.upper(), "stance": stance, "thesis": thesis,
            "catalyst": catalyst, "catalyst_date": catalyst_date,
            "sell_above": sell_above, "stop_below": stop_below,
            "review_by": review_by, "added": date.today().isoformat(),
            "status": "OPEN", "outcome": ""}


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def due_soon(entry: dict, today: date, horizon_days: int = 7) -> bool:
    """True if an OPEN call's catalyst or review date is within the horizon."""
    if entry.get("status", "OPEN") != "OPEN":
        return False
    edge = today + timedelta(days=horizon_days)
    for k in ("catalyst_date", "review_by"):
        d = entry.get(k)
        if d:
            try:
                if date.fromisoformat(str(d)[:10]) <= edge:
                    return True
            except ValueError:
                pass
    return False


def alert_rules_from(rows: list[dict]) -> list[dict]:
    """Turn open exit bands into watcher rules.

    Returns dicts {symbol, label, conditions, mode}. Sell-above and stop-below
    become edge rules (fire once on crossing). Callers replace every existing
    rule tagged with _RULE_TAG so re-arming stays idempotent.
    """
    out = []
    for a in rows:
        if a.get("status", "OPEN") != "OPEN":
            continue
        sym = a["symbol"]
        hi, lo = _num(a.get("sell_above")), _num(a.get("stop_below"))
        if hi:
            out.append({"symbol": sym, "exchange": "NSE",
                        "label": f"exit ≥ ₹{hi:g} · {sym} {_RULE_TAG}",
                        "conditions": [{"metric": "price", "op": ">", "value": hi}],
                        "mode": "edge"})
        if lo:
            out.append({"symbol": sym, "exchange": "NSE",
                        "label": f"stop ≤ ₹{lo:g} · {sym} {_RULE_TAG}",
                        "conditions": [{"metric": "price", "op": "<", "value": lo}],
                        "mode": "edge"})
    return out


def is_advice_rule(label: str | None) -> bool:
    return bool(label) and label.strip().endswith(_RULE_TAG)
