"""Bridge between the local SQLite store and repo-committed JSON, so the GitHub
Actions alert watcher (which has no persistent disk) can share state.

Split by writer to avoid merge conflicts:
  - state/watchlist.json, state/rules.json  → written by the LOCAL dashboard
                                               (control panel), read by the Action.
  - state/alert_state.json, state/alerts_log.json → written ONLY by the Action
                                               (cooldown timestamps + fired log).

The Action run is:  import  →  watcher.run_once()  →  export  →  git commit state.
Because the two sides touch different files, `git pull --rebase` merges cleanly.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import db
from .config import ROOT

STATE_DIR = ROOT / "state"
WATCHLIST_JSON = STATE_DIR / "watchlist.json"
RULES_JSON = STATE_DIR / "rules.json"
ALERT_STATE_JSON = STATE_DIR / "alert_state.json"
ALERTS_LOG_JSON = STATE_DIR / "alerts_log.json"


def _write(path: Path, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def rule_key(r: dict) -> str:
    """Stable content key for a rule (DB ids aren't stable across Action runs)."""
    raw = f"{r['symbol']}|{r['exchange']}|{r.get('label','')}|" \
          f"{json.dumps(r['conditions'], sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ---- called by the LOCAL dashboard whenever watchlist/rules change ---------

def export_config() -> None:
    _write(WATCHLIST_JSON, db.get_watchlist())
    _write(RULES_JSON, [
        {"symbol": r["symbol"], "exchange": r["exchange"], "label": r.get("label"),
         "conditions": r["conditions"], "active": r["active"], "mode": r.get("mode", "level")}
        for r in db.get_rules(active_only=False)
    ])


# ---- called by the ACTION before the watcher runs -------------------------

def import_from_repo() -> None:
    """Rebuild the (ephemeral) SQLite from committed JSON + restore cooldowns."""
    db.init_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM watchlist")
        conn.execute("DELETE FROM alert_rules")

    for w in _read(WATCHLIST_JSON, []):
        db.add_to_watchlist(w["symbol"], w.get("exchange", "NSE"), w.get("name"))

    saved = _read(ALERT_STATE_JSON, {})
    for r in _read(RULES_JSON, []):
        if not r.get("active", 1):
            continue
        rid = db.add_rule(r["symbol"], r.get("exchange", "NSE"),
                          r.get("label") or "alert", r["conditions"], mode=r.get("mode", "level"))
        st = saved.get(rule_key(r))
        if isinstance(st, str):                      # legacy format: bare timestamp
            db.set_last_triggered(rid, st)
        elif isinstance(st, dict):
            if st.get("triggered"):
                db.set_last_triggered(rid, st["triggered"])
            if st.get("state") is not None:
                db.set_last_state(rid, bool(st["state"]))


# ---- called by the ACTION after the watcher runs -------------------------

def export_state() -> None:
    """Persist cooldown timestamps + append the fired log for the next run."""
    rules = db.get_rules(active_only=False)
    _write(ALERT_STATE_JSON, {
        rule_key(r): {"triggered": r.get("last_triggered"), "state": r.get("last_state")}
        for r in rules if r.get("last_triggered") or r.get("last_state") is not None
    })

    log = _read(ALERTS_LOG_JSON, [])
    seen = {(e["ts"], e["message"]) for e in log}
    for h in db.get_alert_history(limit=100):
        key = (h["ts"], h["message"])
        if key not in seen:
            log.append({"ts": h["ts"], "symbol": h["symbol"],
                        "message": h["message"], "channels": h["channels"]})
    _write(ALERTS_LOG_JSON, log[-200:])          # keep the last 200


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        import_from_repo(); print("imported watchlist/rules from repo")
    elif cmd == "export":
        export_state(); print("exported alert state to repo")
    elif cmd == "config":
        export_config(); print("exported watchlist/rules to repo")
    else:
        print("usage: python -m src.repo_state [import|export|config]")
