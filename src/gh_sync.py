"""Push state/*.json to GitHub via the Contents API.

Why this exists: on Streamlit Cloud the repo checkout can't `git push`, so
changes made from the hosted app (your phone) would die on the next redeploy.
With a fine-grained GitHub token (Contents: read/write on this one repo) in
STOCKWATCH_GH_TOKEN, the app commits its state files through the API instead —
same files, same repo, no extra database.

Unchanged files are skipped, so a no-op sync costs a few GETs and zero commits.
"""
from __future__ import annotations

import base64
import os

import requests

from .repo_state import STATE_DIR

REPO = os.environ.get("STOCKWATCH_GH_REPO", "jyoti369/stock-watcher")
FILES = ["watchlist.json", "rules.json", "holdings.json", "suggestions_history.json"]
API = "https://api.github.com"


def token() -> str:
    return os.environ.get("STOCKWATCH_GH_TOKEN", "")


def available() -> bool:
    return bool(token())


def push_state() -> tuple[bool, str]:
    """Commit changed state files. Returns (ok, human message)."""
    tk = token()
    if not tk:
        return False, "no GitHub token configured"
    hdrs = {"Authorization": f"Bearer {tk}", "Accept": "application/vnd.github+json"}
    pushed: list[str] = []
    try:
        for name in FILES:
            path = STATE_DIR / name
            if not path.exists():
                continue
            local = path.read_text()
            url = f"{API}/repos/{REPO}/contents/state/{name}"
            r = requests.get(url, headers=hdrs, timeout=20)
            sha = None
            if r.status_code == 200:
                d = r.json()
                sha = d.get("sha")
                try:
                    remote = base64.b64decode(d.get("content", "")).decode()
                    if remote.strip() == local.strip():
                        continue                      # unchanged — skip
                except Exception:
                    pass
            body = {"message": f"update {name} (from app)",
                    "content": base64.b64encode(local.encode()).decode()}
            if sha:
                body["sha"] = sha
            pr = requests.put(url, headers=hdrs, json=body, timeout=20)
            if not pr.ok:
                return False, f"{name}: HTTP {pr.status_code} {pr.text[:120]}"
            pushed.append(name)
    except Exception as e:
        return False, str(e)[:150]
    if pushed:
        return True, "synced " + ", ".join(pushed)
    return True, "already up to date"
