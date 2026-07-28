"""Personal finance plan, committed to the repo as ciphertext.

Same trust model as holdings.json: the repo is public, the plan is private, so
with STOCKWATCH_STATE_KEY set the markdown is stored Fernet-encrypted inside
state/finance_plan.json and only key-holding devices (Mac, Streamlit secrets)
can render or edit it. Without a key nothing is ever written — a plaintext
personal plan must not reach a public repo by accident.
"""
from __future__ import annotations

import json
from datetime import datetime

from .repo_state import STATE_DIR, _fernet

PLAN_JSON = STATE_DIR / "finance_plan.json"


def load_plan() -> dict | None:
    """{'updated': str, 'content': str} or None (missing / no key / bad key)."""
    f = _fernet()
    if f is None:
        return None
    try:
        raw = json.loads(PLAN_JSON.read_text())
        return json.loads(f.decrypt(raw["cipher"].encode()).decode())
    except Exception:
        return None


def save_plan(content: str) -> bool:
    """Encrypt and write the plan. False (and no write) when no key is set."""
    f = _fernet()
    if f is None:
        return False
    current = load_plan()
    if current is not None and current.get("content") == content:
        return True                                   # unchanged — no commit churn
    data = {"updated": datetime.now().strftime("%d %b %Y, %H:%M"), "content": content}
    STATE_DIR.mkdir(exist_ok=True)
    PLAN_JSON.write_text(json.dumps(
        {"encrypted": True, "cipher": f.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()}))
    return True
