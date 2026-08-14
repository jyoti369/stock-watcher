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

import base64
import hashlib
import json
import os
from pathlib import Path

from . import db
from .config import DB_PATH, ROOT

STATE_DIR = ROOT / "state"
WATCHLIST_JSON = STATE_DIR / "watchlist.json"
RULES_JSON = STATE_DIR / "rules.json"
HOLDINGS_JSON = STATE_DIR / "holdings.json"
ALERT_STATE_JSON = STATE_DIR / "alert_state.json"
ALERTS_LOG_JSON = STATE_DIR / "alerts_log.json"


def _write(path: Path, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def repo_newer_than_db() -> bool:
    """True when the committed state was written after this machine's DB last
    changed — meaning the other copy of the app (phone/cloud) edited it since.

    Exporting on top of that would silently drop whatever it added: a holding
    entered on the phone vanished from a laptop save exactly this way. So the
    local app re-imports first when it sees this.
    """
    try:
        db_at = DB_PATH.stat().st_mtime
    except OSError:
        return WATCHLIST_JSON.exists()
    newest = max((p.stat().st_mtime for p in (WATCHLIST_JSON, RULES_JSON,
                                             HOLDINGS_JSON) if p.exists()),
                 default=0.0)
    return newest > db_at + 1        # a second of slack for same-write jitter


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---- holdings encryption ---------------------------------------------------
# The repo is public, but holdings = real money positions. With a key set
# (STOCKWATCH_STATE_KEY), holdings.json is committed as Fernet ciphertext;
# only devices holding the key (your Mac, your Streamlit secrets) can read it.

def _fernet():
    key_src = os.environ.get("STOCKWATCH_STATE_KEY", "")
    if not key_src:
        return None
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(key_src.encode()).digest())
    return Fernet(key)


def _read_maybe_enc(path: Path, default):
    """Read a state file that may be Fernet ciphertext or legacy plaintext.

    Returns `default` when the file is missing, or when it's encrypted and no
    (or the wrong) key is available — so a keyless run degrades safely instead
    of crashing, and never sees private contents.
    """
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    if isinstance(raw, dict) and raw.get("encrypted"):
        f = _fernet()
        if f is None:
            return default
        try:
            return json.loads(f.decrypt(raw["cipher"].encode()).decode())
        except Exception:
            return default
    return raw                                         # legacy plaintext


def _write_private(path: Path, data) -> bool:
    """Write JSON encrypted with the state key. With NO key this refuses to
    write (returns False) — private data must never reach the public repo as
    plaintext. Skips the rewrite when the decrypted content is unchanged, so
    Fernet's per-call randomness doesn't create pointless commits.
    """
    f = _fernet()
    if f is None:
        return False
    plaintext = json.dumps(data, ensure_ascii=False)
    # Skip the rewrite only when the file is ALREADY an encrypted envelope whose
    # decrypted content matches — otherwise a legacy plaintext file with matching
    # content would never get migrated to ciphertext.
    try:
        raw = json.loads(path.read_text())
        is_env = isinstance(raw, dict) and raw.get("encrypted")
    except (FileNotFoundError, json.JSONDecodeError):
        is_env = False
    if is_env:
        current = _read_maybe_enc(path, None)
        if current is not None and json.dumps(current, ensure_ascii=False) == plaintext:
            return True
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(
        {"encrypted": True, "cipher": f.encrypt(plaintext.encode()).decode()}))
    return True


def _write_holdings(data: list) -> None:
    _write_private(HOLDINGS_JSON, data)


def _read_holdings_raw() -> list | None:
    """Decrypt-or-parse holdings.json. None if unreadable (no key / bad key)."""
    return _read_maybe_enc(HOLDINGS_JSON, None)


def rule_key(r: dict) -> str:
    """Stable content key for a rule (DB ids aren't stable across Action runs)."""
    raw = f"{r['symbol']}|{r['exchange']}|{r.get('label','')}|" \
          f"{json.dumps(r['conditions'], sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ---- called by the LOCAL dashboard whenever watchlist/rules change ---------

def export_config() -> None:
    # All three describe the portfolio (symbols, lots, and — via advice-armed
    # rules — exit/stop price targets), so all three are written encrypted. The
    # watcher Action holds the key and decrypts them at runtime.
    _write_private(WATCHLIST_JSON, db.get_watchlist())
    _write_holdings([
        {"symbol": h["symbol"], "exchange": h["exchange"], "qty": h["qty"],
         "buy_price": h["buy_price"], "buy_date": h.get("buy_date")}
        for h in db.get_holdings()
    ])
    _write_private(RULES_JSON, [
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
        conn.execute("DELETE FROM holdings")

    for w in _read_maybe_enc(WATCHLIST_JSON, []):
        db.add_to_watchlist(w["symbol"], w.get("exchange", "NSE"), w.get("name"),
                            w.get("added_at"))

    for h in (_read_holdings_raw() or []):             # [] if encrypted and no key (e.g. the Action)
        db.add_holding(h["symbol"], h.get("exchange", "NSE"),
                       h["qty"], h["buy_price"], h.get("buy_date"))

    saved = _read(ALERT_STATE_JSON, {})
    for r in _read_maybe_enc(RULES_JSON, []):
        rid = db.add_rule(r["symbol"], r.get("exchange", "NSE"),
                          r.get("label") or "alert", r["conditions"], mode=r.get("mode", "level"))
        # paused rules are imported too, just inactive: the watcher only reads
        # active ones, and dropping them here used to delete a pause on the next
        # export — the rule would quietly come back to life
        if not r.get("active", 1):
            db.set_rule_active(rid, False)
        st = saved.get(rule_key(r))
        if isinstance(st, str):                      # legacy format: bare timestamp
            db.set_last_triggered(rid, st)
        elif isinstance(st, dict):
            if st.get("triggered"):
                db.set_last_triggered(rid, st["triggered"])
            if st.get("state") is not None:
                db.set_last_state(rid, bool(st["state"]))
            if st.get("true_since"):
                db.set_true_since(rid, st["true_since"])


# ---- called by the ACTION after the watcher runs -------------------------

def export_state() -> None:
    """Persist cooldown timestamps + append the fired log for the next run."""
    rules = db.get_rules(active_only=False)
    # true_since rides along: the Action rebuilds the DB from scratch every run,
    # so without it a week-old condition would introduce itself as brand new
    _write(ALERT_STATE_JSON, {
        rule_key(r): {"triggered": r.get("last_triggered"),
                      "state": r.get("last_state"),
                      "true_since": r.get("true_since")}
        for r in rules if r.get("last_triggered") or r.get("last_state") is not None
    })

    log = _read_maybe_enc(ALERTS_LOG_JSON, [])
    seen = {(e["ts"], e["message"]) for e in log}
    for h in db.get_alert_history(limit=100):
        key = (h["ts"], h["message"])
        if key not in seen:
            log.append({"ts": h["ts"], "symbol": h["symbol"],
                        "message": h["message"], "channels": h["channels"]})
    # fired messages name holdings + live prices, so encrypt (the Action has the key)
    _write_private(ALERTS_LOG_JSON, log[-200:])  # keep the last 200


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
