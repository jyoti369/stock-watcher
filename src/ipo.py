"""Live IPO screener, judged by the house rules.

Strategy this serves: apply only for the listing-day flip. The two inputs that
predict a pop are grey-market premium (unofficial, manipulable — especially in
SME issues) and subscription depth (QIB interest is the anti-manipulation
check, since institutions don't touch rigged books). Hence two rule sets:

  Mainboard: GMP >= 20% holding into the close, total sub >= 15x, QIB >= 5x.
  SME:       GMP >= 35%, total sub >= 25x by day 2, QIB >= 2x.
             (min application ~Rs 2L+, allotment is a lottery — size for it)

Always apply on the LAST day, late morning: allotment odds don't depend on
when you bid, so waiting is free information. One lot per PAN — extra lots
don't raise the odds in an oversubscribed retail book.

Data comes from ipowatch.in's public GMP and subscription pages, parsed
defensively (their tables change shape now and then). Everything degrades to
"no data" rather than crashing the tab.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import requests

GMP_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
SUB_URL = "https://ipowatch.in/ipo-subscription-status-today/"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

RULES = {
    "mainboard": {"gmp_pct": 20.0, "total": 15.0, "qib": 5.0},
    "sme": {"gmp_pct": 35.0, "total": 25.0, "qib": 2.0},
}


def _num(text: str) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text).replace("₹", ""))
    return float(m.group().replace(",", "")) if m else None


def _norm(name: str) -> str:
    """Loose key for joining the two tables: lowercase, drop noise words."""
    out = re.sub(r"\b(ipo|limited|ltd|sme|nse|bse)\b", "", str(name).lower())
    return re.sub(r"[^a-z]", "", out)[:14]


def _tables_with_context(html: str) -> list[tuple[str, list[list[str]]]]:
    """Every <table> as rows-of-cell-text, tagged with the nearest heading."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tbl in soup.find_all("table"):
        heading = ""
        for prev in tbl.find_all_previous(["h1", "h2", "h3", "h4"], limit=1):
            heading = prev.get_text(" ", strip=True)
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                for tr in tbl.find_all("tr")]
        out.append((heading, [r for r in rows if r]))
    return out


def _col(headers: list[str], *needles: str) -> int | None:
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(n in hl for n in needles):
            return i
    return None


def fetch_gmp() -> list[dict]:
    """[{name, gmp, price, gmp_pct, status?}] from the GMP page. [] on failure."""
    try:
        html = requests.get(GMP_URL, timeout=25, headers=_UA).text
    except requests.RequestException:
        return []
    out = []
    for _heading, rows in _tables_with_context(html):
        if len(rows) < 2:
            continue
        head = rows[0]
        c_name = _col(head, "ipo", "company")
        c_gmp = _col(head, "gmp", "premium")
        c_price = _col(head, "price", "band")
        if c_name is None or c_gmp is None:
            continue
        for r in rows[1:]:
            if len(r) <= max(c_name, c_gmp):
                continue
            gmp, price = _num(r[c_gmp]), _num(r[c_price]) if c_price is not None \
                and len(r) > c_price else None
            if not r[c_name] or gmp is None:
                continue
            pct = round(100 * gmp / price, 1) if price else None
            out.append({"name": r[c_name], "gmp": gmp, "price": price,
                        "gmp_pct": pct})
    return out


def fetch_subscriptions() -> list[dict]:
    """[{name, sme, total, qib, nii, retail, close}] from the live-sub page."""
    try:
        html = requests.get(SUB_URL, timeout=25, headers=_UA).text
    except requests.RequestException:
        return []
    out = []
    for heading, rows in _tables_with_context(html):
        if len(rows) < 2:
            continue
        head = rows[0]
        c_name = _col(head, "ipo", "company")
        c_total = _col(head, "total")
        if c_name is None or c_total is None:
            continue
        c_qib, c_nii = _col(head, "qib"), _col(head, "nii", "hni")
        c_ret, c_close = _col(head, "retail", "rii"), _col(head, "close", "date")
        c_type = _col(head, "type", "board")
        sme_table = "sme" in heading.lower()
        for r in rows[1:]:
            if len(r) <= max(c_name, c_total) or not r[c_name]:
                continue
            kind = r[c_type].lower() if c_type is not None and len(r) > c_type \
                else ""
            row = {"name": r[c_name], "total": _num(r[c_total]),
                   "sme": sme_table or "sme" in kind
                   or "sme" in r[c_name].lower()}
            for key, idx in (("qib", c_qib), ("nii", c_nii), ("retail", c_ret)):
                row[key] = _num(r[idx]) if idx is not None and len(r) > idx else None
            row["close"] = r[c_close] if c_close is not None and len(r) > c_close \
                else ""
            out.append(row)
    return out


def _close_date(text: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except ValueError:
            continue
    return None


def screen() -> list[dict]:
    """Join GMP + subscription by name and attach a verdict per house rules.
    Only issues still open (close date today or later) make the list — the
    source table is a running archive of the whole year."""
    gmp = {_norm(g["name"]): g for g in fetch_gmp()}
    today = date.today()
    merged = []
    for s in fetch_subscriptions():
        closes = _close_date(s.get("close", ""))
        if closes is None or closes < today:
            continue
        g = gmp.get(_norm(s["name"]), {})
        row = {**s, "gmp": g.get("gmp"), "price": g.get("price"),
               "gmp_pct": g.get("gmp_pct")}
        row["verdict"], row["why"] = verdict(row)
        merged.append(row)
    order = {"APPLY-ZONE": 0, "WATCH": 1, "SKIP": 2, "NO DATA": 3}
    merged.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["gmp_pct"] or 0)))
    return merged


def verdict(row: dict) -> tuple[str, str]:
    """House-rules call on one IPO: APPLY-ZONE / WATCH / SKIP / NO DATA."""
    rules = RULES["sme" if row.get("sme") else "mainboard"]
    pct, total, qib = row.get("gmp_pct"), row.get("total"), row.get("qib")
    if pct is None and total is None:
        return "NO DATA", "no GMP or subscription figures found"
    misses, notes = [], []
    if pct is None:
        misses.append("GMP unknown")
    elif pct < rules["gmp_pct"]:
        misses.append(f"GMP {pct:g}% < {rules['gmp_pct']:g}% bar")
    else:
        notes.append(f"GMP {pct:g}% ok")
    if total is None or total < rules["total"]:
        misses.append(f"subscription {total if total is not None else '?'}x "
                      f"< {rules['total']:g}x")
    else:
        notes.append(f"{total:g}x subscribed")
    if qib is not None and qib < rules["qib"]:
        misses.append(f"QIB only {qib:g}x (want {rules['qib']:g}x+)")
    elif qib is not None:
        notes.append(f"QIB {qib:g}x")

    if not misses:
        return "APPLY-ZONE", "; ".join(notes) + " — apply LAST day, 1 lot"
    # GMP already there but the book still filling = the classic day-1/2 state
    if pct is not None and pct >= rules["gmp_pct"]:
        return "WATCH", "GMP qualifies; recheck subscription on the last day (" \
            + "; ".join(misses) + ")"
    return "SKIP", "; ".join(misses)


def digest_lines(max_rows: int = 6) -> list[str]:
    """Short lines for the morning Telegram brief. [] if nothing is open."""
    rows = screen()
    out = []
    for r in rows[:max_rows]:
        kind = "SME" if r.get("sme") else "MB"
        pct = f"{r['gmp_pct']:g}%" if r.get("gmp_pct") is not None else "?"
        tot = f"{r['total']:g}x" if r.get("total") is not None else "?"
        out.append(f"[{r['verdict']}] {r['name']} ({kind}) — GMP {pct}, "
                   f"sub {tot}, closes {r.get('close') or '?'}")
    return out
