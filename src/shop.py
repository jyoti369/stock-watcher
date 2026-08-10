"""Buy advisor — search Amazon.in + Flipkart and judge results by house rules.

The rules (learned shopping the hard way, Aug 2026):

  1. Trust rating x review depth, never the discount badge. 4.0*+ across
     1,000+ reviews is a mass-proven product; 4.5* across 40 reviews is noise.
  2. Fake-MRP check: "80% off Rs 4,999" on a Rs 900 shoe is an anchor trick —
     the MRP was never real. When MRP >= 3x the selling price, ignore the
     discount entirely and judge the product on its actual price.
  3. Rating floor 3.8*. Below that, no price is cheap enough — budget items
     live or die on the average unit being fine, not the lucky one.
  4. Under 100 reviews = unproven, whatever the stars say.
  5. Prefer listings whose brand you can name; no-name marketplace brands are
     where the fake MRPs and one-month soles cluster.

Sourcing note: Amazon blocks requests coming from datacenter IPs (Streamlit
Cloud, GitHub Actions) with a 503 — the scrape only works from a residential
connection (running the app locally / a laptop session). Flipkart is usually
reachable from anywhere. Everything degrades to "open this search yourself"
links rather than crashing the tab.
"""
from __future__ import annotations

import re
import subprocess
from urllib.parse import quote_plus

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/17.4 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _get(url: str, ua: str | None = None) -> str:
    """Fetch through curl, not requests — both stores fingerprint the TLS
    handshake of python's ssl stack and reject it (Amazon 503, Flipkart 403)
    even from a residential IP, while curl's handshake passes as a browser.
    Flipkart additionally 403s Safari/Chrome UA strings but lets Firefox in.
    Amazon also soft-throttles: an over-eager client gets a ~2KB robot-check
    page served as a 200 — a tiny body is a failure, retried after a pause."""
    import time
    headers = dict(_UA, **({"User-Agent": ua} if ua else {}))
    cmd = ["curl", "-s", "--compressed", "--max-time", "25",
           "-w", "\n%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        try:
            raw = subprocess.run(cmd + [url], capture_output=True, text=True,
                                 timeout=30).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        body, _, code = raw.rpartition("\n")
        if code.strip() == "200" and len(body) > 20000:
            return body
    return ""

RULES = {"rating_floor": 3.8, "pick_rating": 4.0, "pick_reviews": 1000,
         "min_reviews": 100, "fake_mrp_ratio": 3.0}


def search_urls(query: str) -> dict:
    """Hand-openable search pages, for when scraping is blocked."""
    q = quote_plus(query)
    return {
        "Amazon": f"https://www.amazon.in/s?k={q}",
        "Flipkart": f"https://www.flipkart.com/search?q={q}",
    }


def _num(text: str) -> float | None:
    m = re.search(r"\d[\d,]*\.?\d*", str(text))
    return float(m.group().replace(",", "")) if m else None


def search_amazon(query: str, max_price: float | None = None) -> list[dict]:
    """[{title, price, mrp, rating, reviews, url, source}] — [] when blocked."""
    return _parse_amazon(_get(f"https://www.amazon.in/s?k={quote_plus(query)}"),
                         max_price)


def _parse_amazon(html: str, max_price: float | None = None) -> list[dict]:
    from bs4 import BeautifulSoup
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for card in soup.select('div[data-component-type="s-search-result"]'):
        title_el = card.select_one("h2 span")
        price_el = card.select_one("span.a-price-whole")
        link_el = card.select_one("a.a-link-normal[href*='/dp/']")
        if not (title_el and price_el and link_el):
            continue
        title = title_el.get_text(strip=True)
        img = card.select_one("img[alt]")
        alt = re.sub(r"^Sponsored Ad - ", "", img.get("alt", "")) if img else ""
        if len(alt) > len(title):          # h2 often carries just the brand
            title = f"{title} {alt}" if alt.lower().find(title.lower()) < 0 \
                and len(title) < 15 else alt
        price = _num(price_el.get_text(strip=True))
        if price is None or title in seen:
            continue
        if max_price and price > max_price:
            continue
        seen.add(title)
        mrp_el = card.select_one("span.a-price.a-text-price span.a-offscreen")
        rating_el = card.select_one("span.a-icon-alt")
        rating = _num(rating_el.get_text()[:4]) if rating_el else None
        reviews = None
        for el in card.select("a[aria-label], span[aria-label]"):
            m = re.match(r"([\d,]+)\s+ratings?", el.get("aria-label", ""))
            if m:
                reviews = _num(m.group(1))
                break
        if reviews is None:
            for el in card.select("span.a-size-base.s-underline-text"):
                t = el.get_text(strip=True).strip("()")
                if re.fullmatch(r"[\d,]+", t):
                    reviews = _num(t)
                    break
        url = "https://www.amazon.in" + link_el["href"].split("/ref=")[0]
        out.append({"title": title, "price": price,
                    "mrp": _num(mrp_el.get_text()) if mrp_el else None,
                    "rating": rating, "reviews": reviews,
                    "url": url, "source": "Amazon"})
    return out


def search_flipkart(query: str, max_price: float | None = None) -> list[dict]:
    """Best-effort card scrape of Flipkart search; their markup is obfuscated
    so this leans on structure (product links + rupee amounts), not classes."""
    html = _get(f"https://www.flipkart.com/search?q={quote_plus(query)}",
                ua="Mozilla/5.0 (X11; Linux x86_64; rv:109.0) "
                   "Gecko/20100101 Firefox/121.0")
    return _parse_flipkart(html, max_price)


def _parse_flipkart(html: str, max_price: float | None = None) -> list[dict]:
    from bs4 import BeautifulSoup
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for card in soup.select("div[data-id]"):
        link = card.select_one("a[href*='/p/itm']")
        if not link:
            continue
        href = link["href"].split("?")[0]
        if href in seen:
            continue
        img = card.select_one("img[alt]")
        text = card.get_text(" ", strip=True)
        title = (img.get("alt", "") if img else "") or link.get("title", "")
        if not title:
            for a in card.select("a[href*='/p/itm']"):
                if a.get_text(strip=True):
                    title = a.get_text(" ", strip=True)
                    break
        # last resort: card text up to the first rupee figure is the name
        title = (title or text.split("₹")[0]).strip()[:90]
        prices = [_num(p) for p in re.findall(r"₹\s?[\d,]+", text)]
        prices = [p for p in prices if p]
        if not title or not prices:
            continue
        price = min(prices)
        if max_price and price > max_price:
            continue
        mrp = max(prices) if max(prices) > price else None
        m = re.search(r"(\d\.\d)\s*(?:★|stars?)?\s*\(?([\d,]+)?", text)
        rating = float(m.group(1)) if m and 1.0 <= float(m.group(1)) <= 5.0 \
            else None
        reviews = _num(m.group(2)) if m and m.group(2) else None
        seen.add(href)
        out.append({"title": title[:90], "price": price, "mrp": mrp,
                    "rating": rating, "reviews": reviews,
                    "url": "https://www.flipkart.com" + href,
                    "source": "Flipkart"})
    return out


def judge(row: dict) -> tuple[str, str]:
    """House-rules call on one listing: PICK-ZONE / OK / RISKY / AVOID / UNRATED."""
    rating, reviews = row.get("rating"), row.get("reviews")
    price, mrp = row.get("price"), row.get("mrp")
    notes = []
    if mrp and price and mrp >= RULES["fake_mrp_ratio"] * price:
        notes.append("MRP looks inflated — ignore the discount %, judge on "
                     f"the Rs {price:g} you actually pay")
    if rating is None:
        return "UNRATED", "no rating data pulled — open the page and check " \
            "reviews before paying" + ("; " + notes[0] if notes else "")
    if rating < RULES["rating_floor"]:
        return "AVOID", f"{rating:g}★ is below the {RULES['rating_floor']}★ " \
            "floor — at budget prices the average unit has to be good"
    if rating >= RULES["pick_rating"] and (reviews or 0) >= RULES["pick_reviews"]:
        why = f"{rating:g}★ across {int(reviews):,} reviews — mass-proven"
        return "PICK-ZONE", why + ("; " + notes[0] if notes else "")
    if (reviews or 0) < RULES["min_reviews"]:
        return "RISKY", f"{rating:g}★ but " \
            + (f"only {int(reviews)} reviews" if reviews else "review count unknown") \
            + " — too thin to trust the stars" + ("; " + notes[0] if notes else "")
    return "OK", f"{rating:g}★, {int(reviews):,} reviews — decent, not " \
        "top-shelf" + ("; " + notes[0] if notes else "")


def advise(query: str, max_price: float | None = None) -> list[dict]:
    """Merged + judged results from both stores, best first."""
    rows = search_amazon(query, max_price) + search_flipkart(query, max_price)
    for r in rows:
        r["verdict"], r["why"] = judge(r)
    order = {"PICK-ZONE": 0, "OK": 1, "UNRATED": 2, "RISKY": 3, "AVOID": 4}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9),
                             -((r.get("rating") or 0) * 1000
                               + min(r.get("reviews") or 0, 99000) / 100)))
    return rows
