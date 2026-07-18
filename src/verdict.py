"""Plain-English 'bottom line' — synthesises the signals the app already computed
into one honest read a non-expert can act on.

It is deliberately NOT a buy/sell call. It characterises the setup ("quality but
pricey", "cheap but deteriorating", …) from real data, says what would make it
more interesting, and leaves the decision (and position sizing) to the user.
"""
from __future__ import annotations

from typing import Any


def build(score: dict, metrics: dict, val: dict | None, peer: dict | None) -> dict[str, Any]:
    rating = score.get("rating", "Unknown")
    deep = score.get("deep", {}) or {}

    # --- read each dimension ---
    quality = {"OK": "financially solid", "Mixed": "a mixed financial picture",
               "Weak": "weak fundamentals"}.get(rating, "an unclear financial picture")

    pe_verdict = (peer or {}).get("verdict", {}).get("pe")   # cheaper/pricier/in line
    val_pct = val.get("percentile") if val else None
    cheap = (val_pct is not None and val_pct <= 35) or pe_verdict == "cheaper than peers"
    expensive = (val_pct is not None and val_pct >= 70) or pe_verdict == "pricier than peers"

    vs200 = metrics.get("price_vs_ma200")
    below200 = vs200 is not None and vs200 < 0
    qe = deep.get("q_earnings_yoy")
    declining = (qe is not None and qe < 0) or below200
    good = rating == "OK"

    # --- supporting points (the 'why') ---
    points = []
    hp = f" (health {score['score']}/100)" if score.get("score") is not None else ""
    points.append(f"**Business:** {quality}{hp}.")
    if val_pct is not None:
        vw = "cheap" if val_pct <= 30 else "expensive" if val_pct >= 70 else "mid-range"
        extra = f", and {pe_verdict}" if pe_verdict and pe_verdict != "in line with peers" else ""
        points.append(f"**Valuation:** {vw} vs its own 5-year range{extra}.")
    elif pe_verdict:
        points.append(f"**Valuation:** {pe_verdict}.")
    if vs200 is not None:
        points.append(f"**Trend:** {'below' if below200 else 'above'} its 200-day average "
                      f"({vs200:+.0f}%) — {'downtrend' if below200 else 'uptrend'}.")
    if qe is not None:
        points.append(f"**Momentum:** latest quarter profit {qe:+.0f}% YoY.")

    # --- headline stance + what would improve it ---
    if good and cheap and not below200:
        stance = "Screens well — a solid business at a reasonable price and trending up. Worth a closer look."
        watch = "Keep an eye that the valuation doesn't run ahead of the fundamentals."
    elif good and expensive:
        stance = "Quality business, but priced richly — a lot of good news is already in the price."
        watch = "A pullback toward its historical average would give a better entry."
    elif good and declining:
        stance = "Solid company in a soft patch — the fundamentals hold up but momentum is weak right now."
        watch = "Wait for earnings/price momentum to turn back up before it looks compelling."
    elif expensive:                                  # not OK-rated
        stance = ("Weak fundamentals and a rich price — this is the high-risk end." if rating == "Weak"
                  else "A mixed financial picture and a full price — limited margin for error here.")
        watch = "Would need both a cheaper valuation and firmer fundamentals to get interesting."
    elif cheap:                                      # not OK-rated
        stance = ("Cheap, but the fundamentals are shaky — could be a value trap." if rating == "Weak"
                  else "Cheap-ish, but the financials are mixed — check if it's a bargain or a trap.")
        watch = "Look for the earnings trend to stabilise before trusting the low price."
    else:
        stance = "A mixed picture — no clear edge in the data either way right now."
        watch = "Wait for the fundamentals or the valuation to tilt clearly one way."

    return {"stance": stance, "points": points, "watch": watch,
            "caveat": "This is a read of the data, not advice. Decide and size it yourself."}
