"""Personal finance plan, committed to the repo as ciphertext.

Same trust model as holdings.json: the repo is public, the plan is private, so
with STOCKWATCH_STATE_KEY set the markdown is stored Fernet-encrypted inside
state/finance_plan.json and only key-holding devices (Mac, Streamlit secrets)
can render or edit it. Without a key nothing is ever written — a plaintext
personal plan must not reach a public repo by accident.
"""
from __future__ import annotations

import json
import re

from . import clock
from .repo_state import STATE_DIR, _fernet

PLAN_JSON = STATE_DIR / "finance_plan.json"

_CHECK = re.compile(r"^(\s*[-*]\s*\[)([ xX])(\]\s*)(.*)$")


def checklist_items(content: str) -> list[dict]:
    """Every markdown checkbox line: {line: int, text: str, done: bool}."""
    out = []
    for i, ln in enumerate(content.splitlines()):
        m = _CHECK.match(ln)
        if m:
            out.append({"line": i, "text": m.group(4).strip(),
                        "done": m.group(2).lower() == "x"})
    return out


def set_check(content: str, line: int, done: bool) -> str:
    """Flip one checkbox (by line index) to done/undone; content unchanged if the
    line isn't a checkbox. Rewrites only that line, so the rest of the plan and
    its formatting are untouched."""
    lines = content.splitlines()
    if not (0 <= line < len(lines)):
        return content
    m = _CHECK.match(lines[line])
    if not m:
        return content
    lines[line] = f"{m.group(1)}{'x' if done else ' '}{m.group(3)}{m.group(4)}"
    trailing = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + trailing


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
    data = {"updated": clock.stamp(), "content": content}
    STATE_DIR.mkdir(exist_ok=True)
    PLAN_JSON.write_text(json.dumps(
        {"encrypted": True, "cipher": f.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()}))
    return True
