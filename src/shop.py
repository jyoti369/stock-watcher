"""Buy advisor — search Amazon.in / Flipkart / Myntra, judge by house rules,
and boil everything down to ONE score per listing so a decision takes seconds.

The rules (learned shopping the hard way, Aug 2026):

  1. Trust rating x review depth, never the discount badge. 4.0*+ across
     1,000+ reviews is a mass-proven product; 4.5* across 40 reviews is noise.
  2. Fake-MRP check: "80% off Rs 4,999" on a Rs 900 shoe is an anchor trick —
     the MRP was never real. When MRP >= 3x the selling price, ignore the
     discount entirely and judge the product on its actual price.
  3. Rating floor 3.8*. Below that, no price is cheap enough — budget items
     live or die on the average unit being fine, not the lucky one.
  4. Under 100 reviews = unproven, whatever the stars say.
  5. Results must actually MATCH the query — a "solid" tee returned for an
     "anime printed" search is filler, drop it before it wastes attention.

The score (0-100) folds those into one number:
     45% quality  — the rating, Bayesian-shrunk toward 3.7* so a handful of
                    5* reviews can't beat thousands of 4.1* ones
     30% relevance — how much of the query the title actually covers
     15% value     — where the price sits inside the budget
     10% depth     — review count on a log scale (1L >> 1k >> 10)
     minus a nick for fake-MRP anchors.

Sourcing mechanics (each store needed its own trick):
  - python requests' TLS handshake is fingerprinted and rejected by all of
    them — every fetch shells out to curl instead.
  - Amazon 503s datacenter IPs (Streamlit Cloud, Actions) and soft-throttles
    even home IPs with a ~2KB robot page served as HTTP 200 (tiny body =
    retry). When direct fails, fall back to the r.jina.ai reader proxy,
    which fetches from its own infra and returns markdown — that path works
    from the cloud too.
  - Flipkart 403s Safari/Chrome UA strings but admits Firefox; its search
    cards don't carry ratings (JS-loaded), so the top few relevant hits get
    enriched from their product pages' JSON-LD, which is server-rendered.
  - Myntra embeds the full search payload as JSON in window.__myx — ratings
    included, no enrichment needed.
Everything degrades to "open this search yourself" links rather than crash.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from urllib.parse import quote, quote_plus

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/17.4 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
_FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/121.0"

RULES = {"rating_floor": 3.8, "pick_rating": 4.0, "pick_reviews": 1000,
         "min_reviews": 100, "fake_mrp_ratio": 3.0}

_STOP = {"for", "a", "an", "the", "and", "or", "with", "of", "in", "under",
         "rs", "men", "mens", "women", "womens", "man", "woman", "buy",
         "best", "good", "new"}


def search_urls(query: str) -> dict:
    """Hand-openable search pages, for when scraping is blocked."""
    q = quote_plus(query)
    return {
        "Amazon": f"https://www.amazon.in/s?k={q}",
        "Flipkart": f"https://www.flipkart.com/search?q={q}",
        "Myntra": "https://www.myntra.com/" + quote(query.replace(" ", "-")),
    }


def history_url(title: str) -> str:
    """Price-trend lookup on buyhatke for one listing."""
    return "https://buyhatke.com/search/" + quote(title[:60])


def _num(text: str) -> float | None:
    m = re.search(r"\d[\d,]*\.?\d*", str(text))
    return float(m.group().replace(",", "")) if m else None


def _count(text: str) -> float | None:
    """Review counts like '8.8K', '1.1L', '112,087'."""
    m = re.search(r"([\d,.]+)\s*([KLM]?)", str(text).strip(), re.I)
    if not m or not m.group(1).strip(",."):
        return None
    n = float(m.group(1).replace(",", ""))
    return round(n * {"K": 1e3, "L": 1e5, "M": 1e6}.get(m.group(2).upper(), 1))


def _get(url: str, ua: str | None = None) -> str:
    """curl fetch; tiny 200s (Amazon's disguised robot page) count as misses."""
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


# ---------------------------------------------------------------- relevance

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in _STOP and len(t) > 1]


def relevance(query: str, title: str) -> float:
    """Share of the query's meaningful words the title actually covers."""
    q = _tokens(query)
    if not q:
        return 1.0
    hay = title.lower()
    hits = sum(1 for t in q if t in hay
               or (t.endswith("s") and t[:-1] in hay)
               or (t + "s") in hay)
    return hits / len(q)


# ------------------------------------------------------------------- stores

def search_amazon(query: str, max_price: float | None = None) -> list[dict]:
    """Direct scrape first; r.jina.ai reader as the from-anywhere fallback."""
    rows = _parse_amazon(
        _get(f"https://www.amazon.in/s?k={quote_plus(query)}"), max_price)
    if rows:
        return rows
    md = _get_jina(f"https://www.amazon.in/s?k={quote_plus(query)}")
    return _parse_amazon_md(md, max_price)


def _get_jina(url: str) -> str:
    try:
        raw = subprocess.run(
            ["curl", "-s", "--max-time", "60", "-w", "\n%{http_code}",
             "https://r.jina.ai/" + url],
            capture_output=True, text=True, timeout=70).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    body, _, code = raw.rpartition("\n")
    return body if code.strip() == "200" and len(body) > 5000 else ""


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


def _parse_amazon_md(md: str, max_price: float | None = None) -> list[dict]:
    """Parse the r.jina.ai markdown rendering of an Amazon search page.
    Per product it emits:  ## BRAND / ## [Title](...dp/ASIN...) /
    4.1[_4.1 out of 5 stars_](...)[(8.8K)](...) / Price, product page[₹988..."""
    if not md:
        return []
    out, seen = [], set()
    brand, cur = "", None
    for line in md.splitlines():
        m = re.match(r"## \[(.+?)\]\((https://www\.amazon\.in/[^)]*?/dp/"
                     r"[A-Z0-9]{10})", line)
        if m:
            title = m.group(1).strip()
            if brand and not title.lower().startswith(brand.lower()):
                title = f"{brand} {title}"
            cur = {"title": title, "url": m.group(2), "price": None,
                   "mrp": None, "rating": None, "reviews": None,
                   "source": "Amazon"}
            continue
        m = re.match(r"## (?!\[)(.{2,40})$", line)
        if m:
            brand = m.group(1).strip()
            continue
        if cur is None:
            continue
        m = re.match(r"(\d\.\d)\[_\d\.\d out of 5 stars", line)
        if m:
            cur["rating"] = float(m.group(1))
            mv = re.search(r"\[\(([\d.,]+[KLM]?)\)\]", line)
            cur["reviews"] = _count(mv.group(1)) if mv else None
            continue
        if line.startswith("Price, product page"):
            cur["price"] = _num(re.search(r"₹\s?([\d,]+)", line).group(1)) \
                if re.search(r"₹\s?[\d,]+", line) else None
            mv = re.search(r"M\.R\.P:\s*₹\s?([\d,]+)", line)
            cur["mrp"] = _num(mv.group(1)) if mv else None
            if cur["price"] and cur["title"] not in seen \
                    and not (max_price and cur["price"] > max_price):
                seen.add(cur["title"])
                out.append(cur)
            cur = None
    return out


def search_flipkart(query: str, max_price: float | None = None) -> list[dict]:
    html = _get(f"https://www.flipkart.com/search?q={quote_plus(query)}",
                ua=_FIREFOX)
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
        out.append({"title": title, "price": price, "mrp": mrp,
                    "rating": rating, "reviews": reviews,
                    "url": "https://www.flipkart.com" + href,
                    "source": "Flipkart"})
    return out


def enrich_flipkart(rows: list[dict], limit: int = 6) -> None:
    """Fill missing rating/reviews from the product pages' JSON-LD (their
    search cards load ratings via JS). Mutates rows in place, top-N only."""
    done = 0
    for r in rows:
        if done >= limit:
            break
        if r["source"] != "Flipkart" or r.get("rating") is not None:
            continue
        html = _get(r["url"], ua=_FIREFOX)
        done += 1
        m = re.search(r'"aggregateRating":\{"ratingValue":([\d.]+),'
                      r'"reviewCount":(\d+),"ratingCount":(\d+)', html)
        if m:
            r["rating"] = round(float(m.group(1)), 1)
            r["reviews"] = float(m.group(3))
        time.sleep(1)          # stay under their throttle


def search_myntra(query: str, max_price: float | None = None) -> list[dict]:
    html = _get("https://www.myntra.com/" + quote(query.replace(" ", "-")),
                ua=_FIREFOX)
    return _parse_myntra(html, max_price)


def _parse_myntra(html: str, max_price: float | None = None) -> list[dict]:
    m = re.search(r"window\.__myx = (\{.*)", html or "")
    if not m:
        return []
    try:
        payload, _ = json.JSONDecoder().raw_decode(m.group(1))
    except ValueError:
        return []
    prods = (payload.get("searchData", {}).get("results", {})
             .get("products", []))
    out = []
    for p in prods:
        name = (p.get("productName") or p.get("product") or "").strip()
        brand = (p.get("brand") or "").strip()
        title = name if name.lower().startswith(brand.lower()) \
            else f"{brand} {name}".strip()
        price = p.get("price")
        if not title or not price:
            continue
        if max_price and price > max_price:
            continue
        out.append({"title": title[:90], "price": float(price),
                    "mrp": float(p["mrp"]) if p.get("mrp") else None,
                    "rating": round(p["rating"], 1) if p.get("rating") else None,
                    "reviews": float(p["ratingCount"]) if p.get("ratingCount")
                    else None,
                    "url": "https://www.myntra.com/" + p.get("landingPageUrl", ""),
                    "source": "Myntra"})
    return out


# ----------------------------------------------------------------- judging

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


def score(row: dict, query: str, cap: float | None = None) -> int:
    """One 0-100 number per listing; weights in the module docstring."""
    n = row.get("reviews") or 0
    bayes = ((row.get("rating") or 3.7) * n + 3.7 * 40) / (n + 40)
    qual = max(0.0, min(1.0, (bayes - 3.4) / 1.1))
    rel = relevance(query, row.get("title", ""))
    if cap and row.get("price"):
        value = max(0.0, min(1.0, 1 - 0.8 * row["price"] / cap))
    else:
        value = 0.5
    depth = min(1.0, math.log10(n + 1) / 5)
    pts = 45 * qual + 30 * rel + 15 * value + 10 * depth
    if row.get("mrp") and row.get("price") \
            and row["mrp"] >= RULES["fake_mrp_ratio"] * row["price"]:
        pts -= 4
    return int(round(max(0.0, min(100.0, pts))))


def advise(query: str, max_price: float | None = None) -> list[dict]:
    """All stores -> relevance-filter -> enrich -> score -> best first."""
    rows = (search_amazon(query, max_price)
            + search_flipkart(query, max_price)
            + search_myntra(query, max_price))
    matched = [r for r in rows if relevance(query, r["title"]) >= 0.34]
    if len(matched) >= 3:
        rows = matched
    rows.sort(key=lambda r: (-relevance(query, r["title"]),
                             -(r.get("rating") or 0)))
    enrich_flipkart(rows)
    for r in rows:
        r["verdict"], r["why"] = judge(r)
        r["score"] = score(r, query, max_price)
        r["history"] = history_url(r["title"])
    rows.sort(key=lambda r: -r["score"])
    return rows
