"""Live IPO screener, judged by the house rules.

Strategy this serves: apply only for the listing-day flip. The two inputs that
predict a pop are grey-market premium (unofficial, manipulable — especially in
SME issues) and subscription depth (QIB interest is the anti-manipulation
check, since institutions don't touch rigged books). Hence two rule sets:

  Mainboard: QIB >= 20x, total >= 30x, GMP >= 15%.
  SME:       QIB >= 35x, total >= 100x, GMP >= 25%.
             (min application ~Rs 1L+, allotment is a lottery — size for it)

Those bars are calibrated on 2025+2026 listings, not guessed: see
settings.DEFAULTS["ipo_rules"] for the sample. The short version: neither the
premium nor the book is enough alone — a 35% premium on a thin book still
opened -20%, and a deep book with a middling premium still opened negative —
but the three bars together had zero losing opens across 383 issues.

Timing matters as much as the bars. SME books fill in the last hours of the
last day — LAPL Automotive read 55x total with QIB 3.6x when the app looked on
the morning of 10 Aug 2026 and closed at 241x with QIB 81x. Judge an issue
between 3pm and 3:45pm on its final day, not in the morning: allotment odds
don't depend on when you bid, so waiting is free information.

Those are the shipped defaults; the live numbers come from settings.ipo_rules()
and are editable in the Settings tab.

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

# Fallback bars, used only if settings can't be read at all. The live ones come
# from settings.ipo_rules() — they're a judgement call you tune as tickets
# accumulate, not a constant of nature, so they belong in the Settings tab.
RULES = {
    "mainboard": {"gmp_pct": 15.0, "total": 30.0, "qib": 20.0},
    "sme": {"gmp_pct": 25.0, "total": 100.0, "qib": 35.0},
}


def bars(row: dict) -> dict:
    """The bars that apply to this issue, from settings, SME or mainboard."""
    kind = "sme" if row.get("sme") else "mainboard"
    try:
        from . import settings
        return settings.ipo_rules()[kind]
    except Exception:
        return RULES[kind]


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
    today = clock.ist_today()
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
    now = clock.ist_now()
    out = []
    for row in merged:
        row["source"] = source
        row["window"] = window(row, now)          # decided by the clock, not the date
        # An issue whose window has shut is history: whether you applied or
        # missed it, the next move is Angel One's allotment mail, not this app's.
        # Dropping it here keeps it out of every screen and every digest.
        if row["window"] == "shut":
            continue
        row["verdict"], row["why"] = verdict(row)
        out.append(row)
    order = {"APPLY-ZONE": 0, "WATCH": 1, "SKIP": 2, "NO DATA": 3}
    out.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["gmp_pct"] or 0)))
    return out


def verdict(row: dict) -> tuple[str, str]:
    """House-rules call on one IPO: CLOSED / APPLY-ZONE / WATCH / SKIP / NO DATA.

    CLOSED comes first: once the application window is gone, how well it scores
    against the bars is history, and presenting it as an opportunity is the bug
    that had this app saying "apply before 4 pm today" at 8pm.
    """
    if row.get("window") == "shut":
        return "CLOSED", ("applications are shut — allotment usually shows up "
                          "in 2-3 working days")
    rules = bars(row)
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
        return "APPLY-ZONE", "; ".join(notes) + " — apply late on the LAST day, 1 lot"
    # GMP already there but the book still filling = the classic day-1/2 state.
    # Only until the last day, though: "recheck later" is useless advice when
    # there is no later, and a fat premium on a thin book is precisely the
    # profile that opened below issue price half the time in 2026.
    if (pct is not None and pct >= rules["gmp_pct"]
            and row.get("window") not in ("last-day", "shut")):
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


def window(row: dict, now=None) -> str:
    """Whether you can still apply: 'open' | 'last-day' | 'shut' | 'unknown'.

    The date alone isn't enough. An IPO whose last day is today is over once the
    4pm cutoff passes — reading "apply before 4 pm today" at 8pm is worse than
    useless, it's a decision you can no longer make.
    """
    now = now or clock.ist_now()
    closes = row.get("closes")
    if not isinstance(closes, date):
        return "unknown"
    today = now.date()
    if closes > today:
        return "open"
    if closes < today:
        return "shut"
    return "shut" if clock.past_ipo_cutoff(now) else "last-day"


def closing_phrase(row: dict, now=None) -> str:
    """Where this issue stands, in words — honest about the hour, not just the
    date: 'closes today — last day to apply' before 4pm, 'bids shut at 4 pm
    today' after it."""
    now = now or clock.ist_now()
    state = window(row, now)
    if state == "shut" and row.get("closes") == now.date():
        return "bids shut at 4 pm today — nothing left to do"
    if state == "shut":
        return f"closed {close_day(row, now.date())}"
    if state == "last-day":
        return "closes today — last day to apply"
    return f"closes {close_day(row, now.date())}"


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


def brief(today: date | None = None, now: datetime | None = None) -> dict:
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
    now = now or clock.ist_now()          # injectable so tests don't drift by hour
    today = today or now.date()
    act, watch, skipped, todo = [], [], [], []
    table = []
    for r in rows:
        state = r.get("window") or window(r, now)
        if state == "shut":
            continue                      # past the cutoff = not this app's business
        kind = "SME" if r.get("sme") else "mainboard"
        head = f"{r['name']} ({kind})"
        # a to-do only exists while you can still act: last day AND before 4pm
        actionable = state == "last-day"
        table.append({"name": r["name"], "kind": kind, "verdict": r["verdict"],
                      "numbers": numbers_phrase(r),
                      "closes": closing_phrase(r, now),
                      # `ends` is the column-width version for a narrow table;
                      # `closes` is the sentence. Keep them separate — formatting
                      # the sentence as a date is a crash waiting to happen.
                      "ends": ("TODAY" if actionable else "shut"
                               if state == "shut" else
                               clock.short(r["closes"])
                               if isinstance(r.get("closes"), date) else "?"),
                      "last_day": actionable, "why": r["why"],
                      # raw figures too, so a renderer can lay out its own
                      # columns without reaching back into screen()'s rows
                      "gmp_pct": r.get("gmp_pct"), "total": r.get("total"),
                      "qib": r.get("qib")})
        if r["verdict"] == "APPLY-ZONE":
            act.append(f"{head} — {numbers_phrase(r)} · {closing_phrase(r, now)}")
            if actionable:
                todo.append(f"Apply for {r['name']} ({kind} IPO) — today is the "
                            f"last day, bids close at 4 pm. "
                            f"{numbers_phrase(r, compact=True)}. One lot, one PAN.")
        elif r["verdict"] == "WATCH":
            watch.append(f"{head} — {numbers_phrase(r)} · {closing_phrase(r, now)}"
                         f"\n  {r['why']}")
            if actionable:
                bar = bars(r)
                todo.append(f"Decide on {r['name']} ({kind} IPO) before 4 pm — "
                            f"{numbers_phrase(r, compact=True)}; "
                            f"needs {bar['total']:g}x with QIB {bar['qib']:g}x+.")
        else:
            skipped.append(r["name"])
    upd = next((r["updated"] for r in rows if r.get("updated")), "")
    footer = (str(rows[0].get("source", "?")) + (f", as of {upd}" if upd else ""))
    # a count, not a roll-call: naming five issues you're not applying for is
    # five names to read and nothing to decide
    skip = (f"{len(skipped)} others below the bar." if skipped else None)
    # last-day issues that fail at MIDDAY aren't decided yet: SME books multiply
    # in the final hours (LAPL read 55x at noon and closed 241x), so the noon
    # numbers can't be measured against bars calibrated on closing books. The
    # 15:15 last-call run makes the real decision; the brief just says so.
    pending = len([t for t in table if t["last_day"] and t["verdict"] != "APPLY-ZONE"])
    return {"act": act, "watch": watch, "skip": skip, "footer": footer,
            "todo": todo, "rows": table, "lastday_pending": pending}


def last_call(now: datetime | None = None) -> dict:
    """The 3:15pm decision on anything closing today, judged on near-final books.

    Returns {apply, close, footer}. `apply` = rows passing every bar — act now.
    `close` = rows where exactly one bar misses by a fifth or less — the numbers
    are laid out for a human judgement call, since a book at 3:15 can still add
    the last stretch by 4pm. Anything further off is silence, not a section.
    """
    now = now or clock.ist_now()
    rows = [r for r in screen()
            if (r.get("window") or window(r, now)) == "last-day"]
    out = {"apply": [], "close": [], "footer": ""}
    for r in rows:
        bar, verdict_ = bars(r), r["verdict"]
        if verdict_ == "APPLY-ZONE":
            out["apply"].append(r)
            continue
        vals = {"gmp_pct": r.get("gmp_pct"), "total": r.get("total"),
                "qib": r.get("qib")}
        if any(v is None for v in vals.values()):
            continue
        misses = {k: v for k, v in vals.items() if v < bar[k]}
        if len(misses) == 1:
            k, v = next(iter(misses.items()))
            if v >= 0.8 * bar[k]:
                r["near"] = (k, v, bar[k])
                out["close"].append(r)
    upd = next((r["updated"] for r in rows if r.get("updated")), "")
    if rows:
        out["footer"] = (str(rows[0].get("source", "?"))
                         + (f", as of {upd}" if upd else ""))
    return out
