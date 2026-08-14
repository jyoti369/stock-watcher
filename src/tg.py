"""Telegram-shaped rendering.

Telegram is not email. It's a narrow column on a phone where long prose lines
wrap into a wall, so the digest here is built out of the three things Telegram
does well:

  * <b>bold</b> for the few words that anchor a section,
  * <pre> monospace blocks, which are the only way to get numbers to line up in
    columns on a phone,
  * <blockquote expandable> so a long list (the small holdings) collapses to a
    couple of lines and opens on a tap,

plus inline keyboard buttons, which turn "mark this reminder done" into an
actual button instead of a link buried in text.

Only the *content* is escaped, never the markup — that's the difference from the
email path, where the whole body is escaped because it carries no markup at all.
"""
from __future__ import annotations

from html import escape as esc

LIMIT = 4096                 # Telegram's hard cap on one message
_SYMBOL_W = 11               # long broker names get cut rather than wrapping


def b(text) -> str:
    return f"<b>{esc(str(text))}</b>"


def line(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def money(x: float | int | None, width: int = 0) -> str:
    """Rupees for a monospace column: grouped, no symbol, right-aligned."""
    if not isinstance(x, (int, float)) or x != x:
        return "—".rjust(width)
    import re
    s = f"{abs(round(x)):.0f}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head) + "," + tail
    return (("-" if x < 0 else "") + s).rjust(width)


def pct(x: float | int | None, width: int = 0) -> str:
    if not isinstance(x, (int, float)) or x != x:
        return "—".rjust(width)
    return ("0.0%" if round(x, 1) == 0 else f"{x:+.1f}%").rjust(width)


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A monospace block with columns wide enough for their contents.

    First column left-aligned (names), the rest right-aligned (numbers) — the
    layout a holdings screen wants, and the only one that stays readable when
    Telegram refuses to wrap a <pre> block.
    """
    if not rows:
        return ""
    widths = [max(len(str(headers[i])), *(len(str(r[i])) for r in rows))
              for i in range(len(headers))]
    out = ["  ".join(h.ljust(widths[i]) if i == 0 else h.rjust(widths[i])
                     for i, h in enumerate(headers))]
    for r in rows:
        out.append("  ".join(str(c).ljust(widths[i]) if i == 0
                             else str(c).rjust(widths[i])
                             for i, c in enumerate(r)))
    return "<pre>" + esc("\n".join(out)) + "</pre>"


def collapsed(title: str, body: str) -> str:
    """A section that shows as a couple of lines and opens on a tap."""
    if not body:
        return ""
    return (f"{b(title)}\n<blockquote expandable>{body}</blockquote>")


def short_symbol(name: str) -> str:
    name = str(name)
    return name if len(name) <= _SYMBOL_W else name[:_SYMBOL_W - 1] + "…"


def bullets(lines: list[str], mark: str = "·") -> str:
    return "\n".join(f"{mark} {esc(str(ln))}" for ln in lines)


def clip(text: str, limit: int = LIMIT) -> str:
    """Telegram rejects anything over 4096 characters, so cut at a line break
    and say so rather than losing the message."""
    if len(text) <= limit:
        return text
    note = "\n\n… trimmed — open the app for the rest."
    cut = text[:limit - len(note)]
    return cut[:cut.rfind("\n") if "\n" in cut else len(cut)] + note


def buttons(pairs: list[tuple[str, str]], per_row: int = 1) -> list[list[dict]]:
    """Inline keyboard rows from (label, url) pairs. Telegram shows these as
    real buttons under the message."""
    keys = [{"text": label[:60], "url": url} for label, url in pairs if url]
    return [keys[i:i + per_row] for i in range(0, len(keys), per_row)]
