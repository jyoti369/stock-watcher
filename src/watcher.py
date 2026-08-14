"""The background alert checker.

Run periodically (launchd / cron). For every active rule it gathers the current
values, evaluates the ANDed conditions, and — respecting a per-rule cooldown —
fires an alert through the configured channels and logs it.

A rule's conditions are a list of {metric, op, value}, all of which must hold.
Available metrics are listed in METRICS below, so the dashboard can build a
dropdown instead of making you memorise names.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import alerts, analysis, clock, datasource, db, fmt, portfolio
from .config import CONFIG

# metric key -> human label, used by the dashboard rule builder and the checker
METRICS: dict[str, str] = {
    "price": "Last price (₹)",
    "pct_change_day": "Today's change (%)",
    "ret_1w": "1-week return (%)",
    "ret_1m": "1-month return (%)",
    "ret_3m": "3-month return (%)",
    "ret_1y": "1-year return (%)",
    "rsi14": "RSI (14)",
    "price_vs_ma50": "Price vs 50-day avg (%)",
    "price_vs_ma200": "Price vs 200-day avg (%)",
    "pos_in_52w_range": "Position in 52w range (0=low,100=high)",
    "off_52w_high": "Distance from 52w high (%)",
    "pe": "P/E ratio",
    "pb": "Price / Book",
    "roe": "Return on equity (%)",
    "debt_to_equity": "Debt / Equity (x)",
    "dividend_yield": "Dividend yield (%)",
}

OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}

# metric key -> (what it is in plain words, how to print a value of it).
# The alert mail reads as a sentence, so "gap to the 200-day average price is
# -11.2%, below your -10% line" instead of "price_vs_ma200: -11.2 < -10".
_PLAIN: dict[str, tuple[str, str]] = {
    "price": ("the price", "₹{v:,.0f}"),
    "pct_change_day": ("today's move", "{v:+.1f}%"),
    "ret_1w": ("the last week's return", "{v:+.1f}%"),
    "ret_1m": ("the last month's return", "{v:+.1f}%"),
    "ret_3m": ("the last three months' return", "{v:+.1f}%"),
    "ret_1y": ("the last year's return", "{v:+.1f}%"),
    "rsi14": ("its RSI", "{v:.0f}"),
    "price_vs_ma50": ("the gap between price and its 50-day average", "{v:+.1f}%"),
    "price_vs_ma200": ("the gap between price and its 200-day average", "{v:+.1f}%"),
    "pos_in_52w_range": ("its place in the 1-year range", "{v:.0f}"),
    "off_52w_high": ("how far it is below its 1-year high", "{v:.1f}%"),
    "pe": ("its P/E", "{v:.1f}"),
    "pb": ("its price-to-book", "{v:.1f}"),
    "roe": ("its return on equity", "{v:.1f}%"),
    "debt_to_equity": ("its debt against equity", "{v:.2f}x"),
    "dividend_yield": ("its dividend yield", "{v:.2f}%"),
}
_SIDE = {"<": "under", "<=": "under", ">": "over", ">=": "over", "==": "exactly"}

# jargon that needs one line of explaining, added under the alert that used it
_GLOSS = {
    "rsi14": "RSI is a 0-100 momentum gauge: under 30 means heavily sold off, "
             "over 70 means heavily bought.",
    "pos_in_52w_range": "0 means it's at its 1-year low, 100 at its 1-year high.",
    "pe": "P/E is the price divided by a year's profit per share — how many "
          "years of current profit you're paying for.",
    "pb": "Price-to-book compares the price against the company's net assets.",
    "debt_to_equity": "Debt against equity: over 1x means it owes more than "
                      "the owners have put in.",
}


def plain_reason(metric: str, actual: float, op: str, target: float) -> str:
    """One condition as a readable clause."""
    label, spec = _PLAIN.get(metric, (METRICS.get(metric, metric), "{v:g}"))
    return (f"{label} is {spec.format(v=actual)} — "
            f"{_SIDE.get(op, op)} your {spec.format(v=float(target))} line")


def plain_condition(cond: dict) -> str:
    """The same clause with no live value yet — for listing a rule in the app."""
    metric = cond.get("metric")
    label, spec = _PLAIN.get(metric, (METRICS.get(metric, metric), "{v:g}"))
    try:
        target = spec.format(v=float(cond.get("value")))
    except (TypeError, ValueError):
        target = str(cond.get("value"))
    return f"{label} goes {_SIDE.get(cond.get('op'), str(cond.get('op')))} {target}"


def gather_values(symbol: str, exchange: str = "NSE") -> dict[str, float | None]:
    """Flatten live quote + price metrics + fundamentals into the METRICS keyspace."""
    metrics = analysis.compute_metrics(symbol, exchange)
    fund = datasource.get_fundamentals(symbol, exchange)
    live = datasource.get_live_quote(symbol, exchange)

    def maybe(x, scale=1.0):
        return round(x * scale, 3) if isinstance(x, (int, float)) else None

    # yfinance is inconsistent: returnOnEquity is a fraction (0.47), but recent
    # versions return dividendYield already as a percent (2.95). Normalise.
    dy_raw = fund.get("dividendYield")
    dividend_yield = None
    if isinstance(dy_raw, (int, float)):
        dividend_yield = round(dy_raw * 100 if dy_raw < 1 else dy_raw, 3)

    return {
        "price": live["price"] if live.get("ok") else metrics.get("price"),
        "pct_change_day": live.get("pct_change"),
        "ret_1w": metrics.get("ret_1w"),
        "ret_1m": metrics.get("ret_1m"),
        "ret_3m": metrics.get("ret_3m"),
        "ret_1y": metrics.get("ret_1y"),
        "rsi14": metrics.get("rsi14"),
        "price_vs_ma50": metrics.get("price_vs_ma50"),
        "price_vs_ma200": metrics.get("price_vs_ma200"),
        "pos_in_52w_range": metrics.get("pos_in_52w_range"),
        "off_52w_high": metrics.get("off_52w_high"),
        "pe": fund.get("trailingPE"),
        "pb": fund.get("priceToBook"),
        "roe": maybe(fund.get("returnOnEquity"), 100),
        "debt_to_equity": maybe(fund.get("debtToEquity"), 0.01),
        "dividend_yield": dividend_yield,
    }


def _in_cooldown(last_triggered: str | None) -> bool:
    if not last_triggered:
        return False
    try:
        last = datetime.fromisoformat(last_triggered)
    except ValueError:
        return False
    window = timedelta(minutes=int(CONFIG["alerts"]["cooldown_minutes"]))
    return datetime.now(timezone.utc) - last < window


def evaluate_rule(rule: dict, values: dict) -> tuple[bool, list[str], bool]:
    """Returns (all_conditions_true, human reasons, evaluable).
    evaluable is False when a condition's data is missing — the caller then leaves
    the rule's edge-state untouched instead of treating a data gap as 'false'."""
    reasons = []
    for cond in rule["conditions"]:
        metric, op, target = cond.get("metric"), cond.get("op"), cond.get("value")
        actual = values.get(metric)
        fn = OPS.get(op)
        if actual is None or fn is None:
            return False, [], False        # can't determine — data gap
        if not fn(actual, float(target)):
            return False, [], True         # evaluable, condition simply not met
        reasons.append(plain_reason(metric, actual, op, float(target)))
    return True, reasons, True


def _fired_today(last_triggered: str | None) -> bool:
    ist = clock.to_ist(last_triggered) if last_triggered else None
    return bool(ist and ist.date() == clock.ist_today())


def _holding_note(symbol: str, price: float | None) -> str:
    """'You hold 25 shares at ₹1,320 average — that's -₹3,775 (-11.4%) today.'
    Empty when you don't own it or there's no live price."""
    lots = [h for h in db.get_holdings() if h["symbol"] == symbol.upper()]
    if not lots or price is None:
        return ""
    pos = portfolio.by_symbol([portfolio.lot_row(h, {"price": price}) for h in lots])
    if not pos:
        return ""
    p = pos[0]
    unit = "share" if p["qty"] == 1 else "shares"
    return (f"You hold {p['qty']:g} {unit} at {fmt.inr(p['buy_price'])} average — "
            f"that position is {fmt.signed_inr(p['pnl'])} "
            f"({fmt.pct(p['pnl_pct'])}) at this price.")


def alert_body(rule: dict, values: dict, reasons: list[str],
               true_since: date | None = None) -> str:
    """The alert mail, dated and self-explaining: what matched, what you own,
    and why it may keep arriving."""
    price, day = values.get("price"), values.get("pct_change_day")
    head = f"{rule['symbol']} ({rule['exchange']})"
    if price is not None:
        head += f" — {fmt.inr(price)}"
        if isinstance(day, (int, float)):
            head += f", {fmt.move(day)} today"
    out = [clock.stamp(), "", head,
           f'Your alert "{rule.get("label") or "alert"}" just matched:']
    out += [f"• {r}" for r in reasons]
    gloss = [_GLOSS[c["metric"]] for c in rule.get("conditions", [])
             if c.get("metric") in _GLOSS]
    if gloss:
        out += ["(" + " ".join(dict.fromkeys(gloss)) + ")"]
    note = _holding_note(rule["symbol"], price)
    if note:
        out += ["", note]
    if rule.get("mode", "level") == "level":
        since = ""
        if true_since:
            days = (clock.ist_today() - true_since).days + 1
            since = (" It turned true today." if days <= 1 else
                     f" It has been true since {clock.short(true_since)}"
                     f" — {days} days running.")
        out += ["", "This is a standing rule: it stays true while the condition "
                "holds, so you'll get at most one of these a day until it "
                "clears." + since + " Pause it in the app's Alerts tab if it "
                "isn't telling you anything new."]
    else:
        out += ["", "This is a one-off crossing alert — it won't repeat until "
                "the level is crossed again."]
    out += ["", "(your own rule fired this, it isn't advice — check before acting)"]
    return "\n".join(out)


def run_once(verbose: bool = True) -> list[dict]:
    """Check all active rules once. Returns the alerts that fired this run."""
    db.init_db()
    rules = db.get_rules(active_only=True)
    fired = []
    values_cache: dict[str, dict] = {}

    for rule in rules:
        key = f"{rule['symbol']}:{rule['exchange']}"
        if key not in values_cache:
            values_cache[key] = gather_values(rule["symbol"], rule["exchange"])
        values = values_cache[key]

        fired_now, reasons, evaluable = evaluate_rule(rule, values)
        if not evaluable:
            if verbose:
                print(f"[skip] {rule['symbol']} rule #{rule['id']}: data unavailable")
            continue

        mode = rule.get("mode", "level")
        was_true = rule.get("last_state") == 1
        db.set_last_state(rule["id"], fired_now)     # record every evaluable run

        # remember when a standing condition first turned true, and forget it the
        # moment it clears — that's what lets the mail say "3rd day running"
        today = clock.ist_today()
        true_since = None
        if fired_now:
            try:
                true_since = date.fromisoformat(str(rule.get("true_since"))[:10])
            except ValueError:
                true_since = today
            if not was_true or rule.get("true_since") is None:
                true_since = today
                db.set_true_since(rule["id"], today.isoformat())
        elif rule.get("true_since"):
            db.set_true_since(rule["id"], None)

        should_fire = fired_now
        if mode == "edge" and was_true:
            should_fire = False                       # already true last time — wait for a re-cross
        # a level rule can sit true for weeks; one mail a day is a reminder,
        # every cooldown window is spam
        if should_fire and mode == "level" and _fired_today(rule.get("last_triggered")):
            if verbose:
                print(f"[daily-cap] {rule['symbol']} rule #{rule['id']} already mailed today")
            should_fire = False
        if should_fire and _in_cooldown(rule.get("last_triggered")):
            if verbose:
                print(f"[cooldown] {rule['symbol']} rule #{rule['id']} would fire, suppressed")
            should_fire = False
        if not should_fire:
            continue

        label = rule.get("label") or "alert"
        subject = (f"🔔 {rule['symbol']} · {label} · "
                   f"{clock.short(today)} {clock.clock_time()}")
        body = alert_body(rule, values, reasons, true_since)

        channels = alerts.dispatch(subject, body)
        db.mark_triggered(rule["id"])
        db.log_alert(rule["id"], rule["symbol"], rule["exchange"], f"{label}: " + "; ".join(reasons), channels)
        fired.append({"symbol": rule["symbol"], "label": label, "reasons": reasons, "channels": channels})
        if verbose:
            print(f"[fired] {rule['symbol']} #{rule['id']} -> {channels or 'no channel configured'}")

    if verbose and not fired:
        print(f"checked {len(rules)} rule(s), nothing triggered")
    return fired


if __name__ == "__main__":
    run_once()
