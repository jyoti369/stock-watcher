"""Mutual fund holdings: encrypted at rest, valued live off official AMFI NAVs.

Same trust model as holdings.json — the repo is public, so with
STOCKWATCH_STATE_KEY set the fund list is committed as Fernet ciphertext and
nothing is ever written without a key.

Quantities can't be pulled from any broker API (Angel One's SmartAPI is
demat-only and can't see AMC-direct folios), so units live here and prices
come from AMFI via mfapi.in — the industry body publishes every scheme's NAV
daily, free, no login. A holding whose units aren't known yet (fresh purchase
still allotting) carries only `invested`/`est_value` and shows as an estimate
until the units are filled in.
"""
from __future__ import annotations

import json

import requests

from .repo_state import STATE_DIR, _fernet

MF_JSON = STATE_DIR / "mf_holdings.json"
MFAPI = "https://api.mfapi.in"


# ---- encrypted store -------------------------------------------------------

def load_mf() -> list[dict] | None:
    """List of holdings, or None (missing file / no key / wrong key)."""
    f = _fernet()
    if f is None:
        return None
    try:
        raw = json.loads(MF_JSON.read_text())
        return json.loads(f.decrypt(raw["cipher"].encode()).decode())
    except Exception:
        return None


def save_mf(rows: list[dict]) -> bool:
    """Encrypt and write. False (and no write) when no key is set."""
    f = _fernet()
    if f is None:
        return False
    current = load_mf()
    if current is not None and json.dumps(current, sort_keys=True) == \
            json.dumps(rows, sort_keys=True):
        return True                                   # unchanged — no commit churn
    STATE_DIR.mkdir(exist_ok=True)
    MF_JSON.write_text(json.dumps(
        {"encrypted": True,
         "cipher": f.encrypt(json.dumps(rows, ensure_ascii=False).encode()).decode()}))
    return True


# ---- AMFI NAVs (via mfapi.in) ----------------------------------------------

def latest_nav(code: str | int) -> dict | None:
    """{'nav': float, 'date': 'DD-MM-YYYY'} for an AMFI scheme code, else None."""
    try:
        r = requests.get(f"{MFAPI}/mf/{code}/latest", timeout=12)
        d = r.json()["data"][0]
        return {"nav": float(d["nav"]), "date": d["date"]}
    except Exception:
        return None


def search_schemes(q: str) -> list[dict]:
    """AMFI scheme lookup: [{'code': str, 'name': str}, ...] (may be empty)."""
    try:
        r = requests.get(f"{MFAPI}/mf/search", params={"q": q}, timeout=12)
        return [{"code": str(x["schemeCode"]), "name": x["schemeName"]}
                for x in r.json()]
    except Exception:
        return []


# ---- valuation --------------------------------------------------------------

def value_row(h: dict, nav: dict | None) -> dict:
    """One display row: live value when units+NAV exist, else the estimate."""
    units, invested = h.get("units"), h.get("invested")
    if units and nav:
        value, source = units * nav["nav"], f"live {nav['date']}"
    else:
        value, source = h.get("est_value"), "estimate"
    pnl = (value - invested) if (value is not None and invested) else None
    return {
        "name": h.get("name"), "units": units, "invested": invested,
        "nav": nav["nav"] if nav else None, "value": value, "pnl": pnl,
        "pnl_pct": (pnl / invested * 100) if pnl is not None and invested else None,
        "source": source, "note": h.get("note"),
    }


def parse_mf_with_ai(text: str) -> tuple[list[dict], str | None]:
    """Gemini parse of a pasted MF portfolio page. Returns (rows, error)."""
    import re
    from . import ai_insights
    if not ai_insights.available().get("gemini"):
        return [], "AI parsing needs the Gemini key."
    prompt = (
        "Extract mutual fund holdings from the text below (an Indian MF portfolio "
        "page). Return ONLY a JSON array, no prose, each item: "
        '{"name": "<full scheme name incl. Direct/Regular>", '
        '"invested": <rupees or null>, "current": <current value in rupees or null>, '
        '"units": <unit balance or null>}. '
        "Skip totals/headers/anything that is not a fund.\n\n" + text[:6000])
    try:
        raw = ai_insights._gemini(prompt)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        rows = []
        for d in data if isinstance(data, list) else []:
            if not d.get("name"):
                continue
            rows.append({"name": str(d["name"]).strip(), "code": None,
                         "units": d.get("units"), "invested": d.get("invested"),
                         "est_value": d.get("current"), "note": None})
        return rows, (None if rows else "AI couldn't find funds in that text.")
    except Exception as e:
        return [], f"AI parse failed: {str(e)[:120]}"
