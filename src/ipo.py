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

Data: investorgain.com's live report API first — one call carries GMP,
total/QIB/NII/retail subscription, close date and an update timestamp, and it
refreshes through the day (ipowatch.in's tables turned out to lag a day
behind on last-day numbers, which is exactly when they matter). ipowatch's
GMP + subscription pages stay as the fallback when the API is unreachable.
Everything degrades to "no data" rather than crashing the tab.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import requests

from . import clock

GMP_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
SUB_URL = "https://ipowatch.in/ipo-subscription-status-today/"
# investorgain live subscription report (id 333) — the same JSON their site's
# table loads; month/year/FY path segments select the current slice
IG_URL = ("https://webnodejs.investorgain.com/cloud/v2/report/data-read/"
          "333/1/{m}/{y}/{fy}/0/all?search=&v=1")
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


def _fy(d: date) -> str:
    """Indian financial year path segment, e.g. 2026-08-11 -> '2026-27'."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def _parse_investorgain(data: dict) -> list[dict]:
    """Rows from the report JSON. Values arrive wrapped in display HTML
    (name anchor carries the GMP, Total carries its update time), so this
    picks them out with regexes and skips any row it can't read."""
    out = []
    for r in data.get("reportTableData") or []:
        name_html = str(r.get("Name", ""))
        m = re.search(r'title="([^"]+)"', name_html)
        if not m:
            continue
        row = {"name": m.group(1),
               "sme": "SME" in str(r.get("~IPO_Category", "")).upper()
               or "SME" in re.sub(r"<[^>]+>", " ", name_html)}
        g = re.search(r"GMP:\S*?<b>(--|[\d.]+)</b>\s*\(([\d.]+)%", name_html)
        row["gmp"] = _num(g.group(1)) if g else None
        row["gmp_pct"] = (round(float(g.group(2)), 1) or None) if g else None
        total_html = str(r.get("Total", ""))
        t = re.search(r"<b>([\d.]+)</b>", total_html)
        row["total"] = float(t.group(1)) if t else None
        for key, col in (("qib", "QIB"), ("nii", "NII"), ("retail", "RII")):
            row[key] = _num(r.get(col))
        row["price"] = _num(r.get("IPO Price"))
        u = re.search(r"(\d{1,2}(?:st|nd|rd|th)?\s+\w{3,9}\s+\d{1,2}:\d{2})",
                      re.sub(r"<[^>]+>", " ", total_html))
        row["updated"] = u.group(1) if u else ""
        try:
            closes = date.fromisoformat(str(r.get("~end_dt", ""))[:10])
        except ValueError:
            closes = _close_date(r.get("Closing Date", ""))
        row["_closes"] = closes
        row["close"] = closes.strftime("%B %d, %Y") if closes else ""
        out.append(row)
    return out


def fetch_investorgain() -> list[dict]:
    """Live rows (see _parse_investorgain). [] on any failure — the caller
    then falls back to the ipowatch pages."""
    today = date.today()
    url = IG_URL.format(m=today.month, y=today.year, fy=_fy(today))
    try:
        return _parse_investorgain(
            requests.get(url, timeout=25, headers=_UA).json())
    except Exception:
        return []


def _close_date(text: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except ValueError:
            continue
    return None


def screen() -> list[dict]:
    """Open IPOs (close date today or later) with a house-rules verdict each.
    investorgain first (live, one call); on failure, join ipowatch's GMP +
    subscription pages by name — that source runs about a day behind."""
    today = clock.ist_today()
    merged = []
    for r in fetch_investorgain():
        closes = r.pop("_closes", None)
        if closes and closes >= today:
            merged.append({**r, "closes": closes})
    source = "investorgain.com (live)"
    if not merged:
        source = "ipowatch.in (can lag a day)"
        gmp = {_norm(g["name"]): g for g in fetch_gmp()}
        for s in fetch_subscriptions():
            closes = _close_date(s.get("close", ""))
            if closes is None or closes < today:
                continue
            g = gmp.get(_norm(s["name"]), {})
            merged.append({**s, "gmp": g.get("gmp"), "price": g.get("price"),
                           "gmp_pct": g.get("gmp_pct"), "updated": "",
                           "closes": closes})
    for row in merged:
        row["source"] = source
        row["verdict"], row["why"] = verdict(row)
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


def close_day(row: dict, today: date | None = None) -> str:
    """The last day in words: 'today', 'Mon 17 Aug (in 3 days)', or whatever
    the source gave us if the date wouldn't parse."""
    closes = row.get("closes")
    if not isinstance(closes, date):
        return str(row.get("close") or "an unknown date")
    today = today or clock.ist_today()
    return "today" if closes == today else clock.when(closes, today)


def closing_phrase(row: dict, today: date | None = None) -> str:
    """'closes today — last day to apply' / 'closes Mon 17 Aug (in 3 days)'."""
    day = close_day(row, today)
    return ("closes today — last day to apply" if day == "today"
            else f"closes {day}")


def numbers_phrase(row: dict, compact: bool = False) -> str:
    """The three numbers that decide it. compact drops the explanations, for a
    to-do line where the detail sits right below anyway."""
    pct = f"{row['gmp_pct']:g}%" if row.get("gmp_pct") is not None else "not quoted"
    if compact:
        bits = [f"premium {pct}"]
        bits.append(f"book {row['total']:g}x" if row.get("total") is not None
                    else "book not reported")
        if row.get("qib") is not None:
            bits.append(f"QIB {row['qib']:g}x")
        return ", ".join(bits)
    bits = [f"grey-market premium {pct}"]
    if row.get("gmp") and row.get("price"):
        bits[0] += f" (₹{row['gmp']:g} over the ₹{row['price']:g} price)"
    bits.append(f"book {row['total']:g}x subscribed" if row.get("total") is not None
                else "book not reported yet")
    if row.get("qib") is not None:
        bits.append(f"big money (QIB) {row['qib']:g}x")
    return " · ".join(bits)


def brief(today: date | None = None) -> dict:
    """The IPO section of the midday mail, grouped by what you'd actually do.

    Returns {act, watch, skip, footer, todo}: `act` are the ones passing every
    bar, `watch` the ones whose premium qualifies but whose book is still
    filling, `skip` a single line naming the rest (a six-row table where five
    rows say SKIP buries the one that matters), and `todo` the short imperative
    for anything whose last day is today.
    """
    rows = screen()
    if not rows:
        return {"act": [], "watch": [], "skip": None, "footer": "", "todo": [],
                "rows": []}
    today = today or clock.ist_today()
    act, watch, skipped, todo = [], [], [], []
    table = []
    for r in rows:
        kind = "SME" if r.get("sme") else "mainboard"
        head = f"{r['name']} ({kind})"
        table.append({"name": r["name"], "kind": kind, "verdict": r["verdict"],
                      "numbers": numbers_phrase(r),
                      "closes": closing_phrase(r, today),
                      "last_day": r.get("closes") == today, "why": r["why"]})
        if r["verdict"] == "APPLY-ZONE":
            act.append(f"{head} — {numbers_phrase(r)} · {closing_phrase(r, today)}")
            if r.get("closes") == today:
                todo.append(f"Apply for {r['name']} ({kind} IPO) — today is the "
                            f"last day, bids close at 4 pm. "
                            f"{numbers_phrase(r, compact=True)}. One lot, one PAN.")
        elif r["verdict"] == "WATCH":
            watch.append(f"{head} — {numbers_phrase(r)} · {closing_phrase(r, today)}"
                         f"\n  {r['why']}")
            if r.get("closes") == today:
                bar = RULES["sme" if r.get("sme") else "mainboard"]
                todo.append(f"Decide on {r['name']} ({kind} IPO) today — bids "
                            f"close at 4 pm. The premium clears the bar but "
                            f"{numbers_phrase(r, compact=True)}. Apply only if the book "
                            f"crosses {bar['total']:g}x with QIB over "
                            f"{bar['qib']:g}x.")
        else:
            pct = f"{r['gmp_pct']:g}%" if r.get("gmp_pct") is not None else "no GMP"
            skipped.append(f"{r['name']} ({pct})")
    upd = next((r["updated"] for r in rows if r.get("updated")), "")
    footer = ("Numbers from " + str(rows[0].get("source", "?"))
              + (f", as of {upd}" if upd else "")
              + ". Bars: mainboard needs 20% premium, 15x book, QIB 5x · "
                "SME needs 35%, 25x, QIB 2x.")
    skip = None
    if skipped:
        skip = (f"Not worth it today ({len(skipped)}): "
                + ", ".join(skipped[:6])
                + (f" and {len(skipped) - 6} more" if len(skipped) > 6 else ""))
    # the HTML mail draws its own table, so it gets the rows too — same numbers,
    # just not pre-strung into sentences
    return {"act": act, "watch": watch, "skip": skip, "footer": footer,
            "todo": todo, "rows": table}
