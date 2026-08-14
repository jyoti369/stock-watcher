"""HTML bodies for the digests and alerts.

Mail clients are a decade behind browsers: Gmail strips <style> blocks,
flexbox and grid, so everything here is tables with inline styles, one column,
capped at 640px so it reads on a phone. Colour is used for one thing only —
money direction — and each number carries its own colour, because a red dot at
the start of a line that also shows a green day was read as "the stock is
down today" when it meant "your position is in loss".

Every renderer takes plain data and returns a fragment; `page()` wraps them.
The mails also go out as plain text (Telegram, and any client that refuses
HTML), so nothing here may carry information the text version lacks.
"""
from __future__ import annotations

from html import escape

INK = "#16191d"
MUTED = "#6b7280"
LINE = "#e3e6ea"
CARD = "#ffffff"
PAGE = "#f4f5f7"
GREEN = "#0f7b3f"
RED = "#c62828"
GREEN_BG = "#eaf6ee"
RED_BG = "#fdecec"
AMBER_BG = "#fff6e5"
AMBER = "#8a5a00"
HEAD_BG = "#14202b"
ACCENT = "#17a398"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
        "Arial,sans-serif")


def money_colour(x: float | int | None) -> str:
    if not isinstance(x, (int, float)) or round(x, 2) == 0:
        return MUTED
    return GREEN if x > 0 else RED


def num(text: str, value: float | int | None, bold: bool = True) -> str:
    """A number coloured by its own direction, nothing else's."""
    weight = "600" if bold else "400"
    return (f'<span style="color:{money_colour(value)};font-weight:{weight};'
            f'white-space:nowrap">{escape(text)}</span>')


def page(title: str, subtitle: str, blocks: list[str], footer: str = "") -> str:
    body = "".join(b for b in blocks if b)
    foot = (f'<div style="font:400 12px/1.6 {FONT};color:{MUTED};padding:14px 4px 0">'
            f'{footer}</div>') if footer else ""
    return f"""<div style="background:{PAGE};padding:16px 8px;font:{FONT}">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="max-width:640px;margin:0 auto">
<tr><td style="background:{HEAD_BG};border-radius:12px 12px 0 0;padding:16px 18px">
<div style="font:600 17px/1.3 {FONT};color:#ffffff">{escape(title)}</div>
<div style="font:400 13px/1.5 {FONT};color:{ACCENT};padding-top:3px">{escape(subtitle)}</div>
</td></tr>
<tr><td style="background:{CARD};border:1px solid {LINE};border-top:none;
 border-radius:0 0 12px 12px;padding:6px 14px 16px">{body}</td></tr>
</table>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="max-width:640px;margin:0 auto"><tr><td>{foot}</td></tr></table>
</div>"""


def section(title: str, inner: str, tone: str = "plain") -> str:
    """A titled block. tone shades the heading for the ones that mean urgency."""
    colour = {"warn": RED, "act": ACCENT, "plain": INK}.get(tone, INK)
    return (f'<div style="padding:14px 0 0">'
            f'<div style="font:600 13px/1.4 {FONT};color:{colour};'
            f'text-transform:uppercase;letter-spacing:.04em">{escape(title)}</div>'
            f'{inner}</div>')


def money_card(totals: dict, fmt) -> str:
    """Portfolio value with today's move and the overall gain, side by side."""
    if not totals or not totals.get("invested"):
        return ""
    def cell(label: str, amount, pct) -> str:
        return (f'<td width="50%" style="padding:8px 10px;vertical-align:top">'
                f'<div style="font:400 12px/1.4 {FONT};color:{MUTED}">{escape(label)}</div>'
                f'<div style="font:600 17px/1.5 {FONT};padding-top:2px">'
                f'{num(fmt.signed_inr(amount), amount)}</div>'
                f'<div style="font:400 12px/1.4 {FONT}">{num(fmt.pct(pct), amount, False)}'
                f'</div></td>')
    missing = ""
    if totals.get("missing"):
        n = totals["missing"]
        missing = (f'<div style="font:400 12px/1.5 {FONT};color:{MUTED};padding:2px 10px 0">'
                   f"{n} holding{'' if n == 1 else 's'} had no price this run, so "
                   f"{'it is' if n == 1 else 'they are'} left out of these totals</div>")
    return f"""<div style="padding:14px 0 0">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
 style="background:{PAGE};border-radius:10px">
<tr><td colspan="2" style="padding:12px 10px 0">
<div style="font:400 12px/1.4 {FONT};color:{MUTED}">Your holdings are worth</div>
<div style="font:600 26px/1.3 {FONT};color:{INK}">{escape(fmt.inr(totals['value']))}</div>
<div style="font:400 12px/1.5 {FONT};color:{MUTED}">you put in
 {escape(fmt.inr(totals['invested']))}</div></td></tr>
<tr>{cell('Today', totals.get('day_move'), totals.get('day_pct'))}
{cell('Since you bought', totals.get('pnl'), totals.get('pnl_pct'))}</tr>
<tr><td colspan="2">{missing}</td></tr></table></div>"""


def stock_rows(positions: list[dict], watch_only: list[dict], tail: dict | None,
               fmt) -> str:
    """One row per stock: name and price on the left, today's move and your own
    position on the right, each coloured for what it is."""
    rows = []

    def row(left_top: str, left_sub: str, right_top: str, right_sub: str) -> str:
        return (f'<tr><td style="padding:9px 2px;border-bottom:1px solid {LINE}">'
                f'<div style="font:600 14px/1.4 {FONT};color:{INK}">{left_top}</div>'
                f'<div style="font:400 12px/1.5 {FONT};color:{MUTED}">{left_sub}</div></td>'
                f'<td align="right" style="padding:9px 2px;border-bottom:1px solid {LINE}">'
                f'<div style="font:400 13px/1.4 {FONT}">{right_top}</div>'
                f'<div style="font:400 12px/1.5 {FONT}">{right_sub}</div></td></tr>')

    def day_cell(pct) -> str:
        if pct is None:
            return f'<span style="color:{MUTED}">—</span>'
        # the arrow sits inside the coloured span, so an up day is a green ▲
        return (f'<span style="color:{money_colour(pct)};white-space:nowrap">'
                f'{fmt.arrow(pct)} {fmt.pct(pct)}</span>')

    for p in positions:
        if p["value"] is None:
            rows.append(row(escape(p["symbol"]), f"you hold {p['qty']:g}",
                            f'<span style="color:{MUTED}">no price</span>', ""))
            continue
        # LTP (what it last traded at) and your own average cost, the two prices
        # a holdings screen is actually read for; ATP only when the exchange
        # published one, and the day's range as the honest stand-in when not
        sub = [f"LTP {escape(fmt.inr(p['price']))}",
               f"avg {escape(fmt.inr(p.get('buy_price')))}"]
        if p.get("atp"):
            sub.append(f"ATP {escape(fmt.inr(p['atp']))}")
        elif p.get("day_low") and p.get("day_high"):
            sub.append(f"day {escape(fmt.inr(p['day_low']))}–"
                       f"{escape(fmt.inr(p['day_high']))}")
        sub.append(f"×{p['qty']:g}")
        rows.append(row(
            escape(p["symbol"]),
            " · ".join(sub) + (" · ⚠️ weak fundamentals" if p.get("weak") else ""),
            f'<span style="color:{MUTED};font-size:12px">today</span> '
            + day_cell(p["day_pct"]),
            num(f"{'up' if p['pnl'] >= 0 else 'down'} {fmt.inr(abs(p['pnl']))} "
                f"({fmt.pct(p['pnl_pct'])})", p["pnl"])))
    if tail:
        rows.append(row(
            f"…and {tail['count']} smaller holdings",
            "listed below" if tail.get("rows") else escape(tail["names"]),
            f'<span style="color:{MUTED};font-size:12px">worth '
            f'{escape(fmt.inr(tail["value"]))}</span>',
            num(f"{'up' if tail['pnl'] >= 0 else 'down'} {fmt.inr(abs(tail['pnl']))}",
                tail["pnl"])))
    for w in watch_only:
        rows.append(row(
            escape(w["symbol"]),
            (f"LTP {escape(fmt.inr(w['price']))}" if w.get("price") is not None
             else "no price") + " · watching, not held"
            + (" · ⚠️ weak fundamentals" if w.get("weak") else ""),
            f'<span style="color:{MUTED};font-size:12px">today</span> '
            + day_cell(w.get("day_pct")), ""))
    if not rows:
        return ""
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
            f' width="100%" style="padding-top:4px">{"".join(rows)}</table>')


def small_holdings(tail: dict | None, fmt) -> str:
    """The long tail, one compact line each — counted AND readable."""
    if not tail or not tail.get("rows"):
        return ""
    cells = []
    for p in tail["rows"]:
        # the rupee figure is what it's WORTH and the percent is your profit on
        # it, so only the percent takes the colour — one coloured pair read as if
        # the value itself had gone up
        right = (f'<span style="color:{MUTED}">worth {escape(fmt.inr(p["value"]))}'
                 f'</span> &nbsp;{num(fmt.pct(p["pnl_pct"]), p["pnl"])}'
                 if p.get("value") is not None
                 else f'<span style="color:{MUTED}">no price</span>')
        cells.append(
            f'<tr><td style="padding:5px 2px;font:400 13px/1.5 {FONT};color:{INK}">'
            f'{escape(p["symbol"])}'
            f'<span style="color:{MUTED}"> ×{p["qty"]:g}</span></td>'
            f'<td align="right" style="padding:5px 2px;font:400 13px/1.5 {FONT}">'
            f'{right}</td></tr>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
            f' width="100%" style="padding-top:2px">{"".join(cells)}</table>')


def todo_cards(items: list[str]) -> str:
    """The same-day to-do list — the one thing that must not be scrolled past."""
    if not items:
        return ""
    cards = []
    for n, t in enumerate(items, 1):
        cards.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
            f' width="100%" style="background:{AMBER_BG};border-radius:10px;'
            f'margin-top:8px"><tr>'
            f'<td width="26" valign="top" style="padding:11px 0 11px 11px;'
            f'font:600 14px/1.5 {FONT};color:{AMBER}">{n}.</td>'
            f'<td style="padding:11px 12px 11px 4px;font:400 14px/1.55 {FONT};'
            f'color:{INK}">{escape(t)}</td></tr></table>')
    return "".join(cards)


def dated_items(items: list[dict], tone: str = "plain") -> str:
    """Reminder-style rows: the text, its date, and — when the item carries a
    `link` — a tap-to-mark-done button, so a finished job can be cleared from
    the mail instead of nagging until you next open the app."""
    if not items:
        return ""
    bg = {"warn": RED_BG, "plain": PAGE}.get(tone, PAGE)
    colour = {"warn": RED, "plain": MUTED}.get(tone, MUTED)
    out = []
    for i in items:
        when = f'<div style="font:400 12px/1.5 {FONT};color:{colour};padding-top:2px">' \
               f'{escape(i["when"])}</div>' if i.get("when") else ""
        action = ""
        if i.get("link"):
            action = (f'<div style="padding-top:8px">'
                      f'<a href="{escape(i["link"])}" style="font:600 12px/1 {FONT};'
                      f'background:{GREEN};color:#ffffff;text-decoration:none;'
                      f'padding:8px 13px;border-radius:16px;display:inline-block">'
                      f'✓ Mark done</a>'
                      f'<span style="font:400 11px/1.6 {FONT};color:{MUTED}">'
                      f'&nbsp;&nbsp;opens the app and ticks it off</span></div>')
        out.append(f'<div style="background:{bg};border-radius:8px;padding:9px 11px;'
                   f'margin-top:8px;font:400 14px/1.55 {FONT};color:{INK}">'
                   f'{escape(i["text"])}{when}{action}</div>')
    return "".join(out)


def bullets(lines: list[str]) -> str:
    if not lines:
        return ""
    items = "".join(f'<li style="padding:3px 0">{escape(ln)}</li>' for ln in lines)
    return (f'<ul style="margin:6px 0 0;padding-left:20px;font:400 14px/1.55 {FONT};'
            f'color:{INK}">{items}</ul>')


def note(text: str) -> str:
    return (f'<div style="font:400 12px/1.6 {FONT};color:{MUTED};padding:8px 0 0">'
            f'{escape(text)}</div>')


def ipo_rows(rows: list[dict]) -> str:
    """One line per open IPO: name and verdict pill, then the three numbers."""
    if not rows:
        return ""
    pill = {"APPLY-ZONE": (GREEN_BG, GREEN, "passes every bar"),
            "WATCH": (AMBER_BG, AMBER, "premium ok, book filling"),
            "SKIP": (PAGE, MUTED, "below the bar"),
            "NO DATA": (PAGE, MUTED, "no numbers yet")}
    out = []
    for r in rows:
        bg, fg, label = pill.get(r["verdict"], (PAGE, MUTED, r["verdict"]))
        out.append(
            f'<tr><td style="padding:9px 2px;border-bottom:1px solid {LINE}">'
            f'<div style="font:600 14px/1.45 {FONT};color:{INK}">{escape(r["name"])}'
            f' <span style="font:400 11px/1.4 {FONT};background:{bg};color:{fg};'
            f'padding:2px 7px;border-radius:20px;white-space:nowrap">{escape(label)}'
            f'</span></div>'
            f'<div style="font:400 12px/1.6 {FONT};color:{MUTED}">{escape(r["kind"])}'
            f' · {escape(r["numbers"])}</div>'
            f'<div style="font:400 12px/1.6 {FONT};color:'
            f'{RED if r.get("last_day") else MUTED}">{escape(r["closes"])}</div>'
            f'</td></tr>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
            f' width="100%" style="padding-top:4px">{"".join(out)}</table>')


def alert_card(symbol: str, exchange: str, label: str, price_line: str,
               reasons: list[str], gloss: str, position: str, repeat: str,
               fmt, day_pct=None) -> str:
    """The single-alert mail: what fired, what you own, why it may repeat."""
    why = "".join(
        f'<div style="padding:4px 0;font:400 14px/1.55 {FONT};color:{INK}">{escape(r)}</div>'
        for r in reasons)
    blocks = [
        f'<div style="padding:14px 0 0">'
        f'<span style="font:600 22px/1.3 {FONT};color:{INK}">{escape(price_line)}</span>'
        + (f'&nbsp;<span style="font:400 14px/1.4 {FONT}">'
           f'{escape(fmt.arrow(day_pct))} {num(fmt.pct(day_pct), day_pct, False)}'
           f'<span style="color:{MUTED}"> today</span></span>'
           if day_pct is not None else "")
        + "</div>",
        section("Why it fired", why + (note(gloss) if gloss else "")),
    ]
    if position:
        blocks.append(
            f'<div style="background:{PAGE};border-radius:10px;padding:11px 12px;'
            f'margin-top:14px;font:400 14px/1.6 {FONT};color:{INK}">{escape(position)}</div>')
    if repeat:
        blocks.append(note(repeat))
    return "".join(blocks)
