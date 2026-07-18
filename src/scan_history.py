"""Suggestion scan history.

Every 'Find suggestions' run saves a compact snapshot (who was picked, at what
price, with what score and stance) to state/suggestions_history.json — which
auto-syncs to GitHub like the rest of the state, so it survives redeploys and
shows up on the cloud dashboard too.

The point isn't nostalgia: storing the price *at scan time* lets the UI answer
"how did last week's picks actually do?" — which keeps the suggestion engine
honest and teaches you how much to trust it.
"""
from __future__ import annotations

import json
from typing import Any

from .repo_state import STATE_DIR

HISTORY_JSON = STATE_DIR / "suggestions_history.json"
MAX_SCANS = 20


def load() -> list[dict[str, Any]]:
    """Newest first. [] if no history yet."""
    try:
        return json.loads(HISTORY_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append(ts: str, params: dict, ranked: list[dict], stances: dict[str, str]) -> None:
    """Store a compact snapshot of one scan (no DataFrames, just display facts)."""
    picks = []
    for r in ranked:
        av = r.get("analyst") or {}
        picks.append({
            "symbol": r["symbol"],
            "name": (r.get("name") or r["symbol"])[:40],
            "score": r.get("score"),
            "health": (r.get("health") or {}).get("rating"),
            "health_score": (r.get("health") or {}).get("score"),
            "price_then": r.get("price"),
            "analyst_target": av.get("target"),
            "stance": stances.get(r["symbol"], ""),
        })
    scans = load()
    scans.insert(0, {"ts": ts, "params": params, "picks": picks})
    STATE_DIR.mkdir(exist_ok=True)
    HISTORY_JSON.write_text(json.dumps(scans[:MAX_SCANS], indent=1, ensure_ascii=False))


def clear() -> None:
    HISTORY_JSON.write_text("[]")
