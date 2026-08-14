"""Money and movement wording, shared by the mails and the dashboard.

Rupees are grouped the Indian way (4,12,340 — not 412,340), because that's how
you read a bank statement, and gains carry a colour that always means the same
thing: green = money made, red = money lost. Never a plain number where a
sign matters.
"""
from __future__ import annotations

import re

# plain geometric arrows, not the emoji triangles: 🔺/🔻 carry their own colour
# (red-ish and blue-ish) which fought the green/red we actually mean. These take
# the colour of the text around them, so an up day reads green in the mail.
UP, DOWN, FLAT = "▲", "▼", "–"
GOOD, BAD, NEUTRAL = "🟢", "🔴", "▪️"


def inr(x: float | int | None) -> str:
    """1234567.4 -> '₹12,34,567'. None -> '—'. Negatives keep the sign
    outside the symbol: '-₹1,890'."""
    if not isinstance(x, (int, float)) or x != x:      # x != x catches NaN
        return "—"
    s = f"{abs(round(x)):.0f}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head) + "," + tail
    return ("-₹" if x < 0 else "₹") + s


def signed_inr(x: float | int | None) -> str:
    """'+₹52,400' / '-₹1,890' — for gains, where the sign is the point."""
    if not isinstance(x, (int, float)):
        return "—"
    return ("+" + inr(x)) if x >= 0 else inr(x)


def pct(x: float | int | None, places: int = 1) -> str:
    """'+14.6%' / '-0.5%' / '—'. A value that rounds to nothing prints as a
    plain 0.0%, never '-0.0%'."""
    if not isinstance(x, (int, float)):
        return "—"
    if round(x, places) == 0:
        return "0%" if places == 0 else f"0.{'0' * places}%"
    return f"{x:+.{places}f}%"


def arrow(x: float | int | None, places: int = 1) -> str:
    """Direction of a move. Judged on the *rounded* number so a +0.04% day
    never shows an up arrow next to a printed 0.0%."""
    if not isinstance(x, (int, float)):
        return FLAT
    r = round(x, places)
    return UP if r > 0 else DOWN if r < 0 else FLAT


def money_dot(x: float | int | None) -> str:
    """Colour for a rupee amount you own: green in profit, red in loss."""
    if not isinstance(x, (int, float)):
        return NEUTRAL
    return GOOD if x > 0 else BAD if x < 0 else NEUTRAL


def move(x: float | int | None, places: int = 1) -> str:
    """'🔻 -0.5%' — arrow plus signed percent, the pair always together."""
    if not isinstance(x, (int, float)):
        return "—"
    return f"{arrow(x, places)} {'0.0%' if round(x, places) == 0 else pct(x, places)}"
