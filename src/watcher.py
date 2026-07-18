"""The background alert checker.

Run periodically (launchd / cron). For every active rule it gathers the current
values, evaluates the ANDed conditions, and — respecting a per-rule cooldown —
fires an alert through the configured channels and logs it.

A rule's conditions are a list of {metric, op, value}, all of which must hold.
Available metrics are listed in METRICS below, so the dashboard can build a
dropdown instead of making you memorise names.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import alerts, analysis, datasource, db
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
        reasons.append(f"{METRICS.get(metric, metric)}: {actual} {op} {target}")
    return True, reasons, True


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

        should_fire = fired_now
        if mode == "edge" and was_true:
            should_fire = False                       # already true last time — wait for a re-cross
        if should_fire and _in_cooldown(rule.get("last_triggered")):
            if verbose:
                print(f"[cooldown] {rule['symbol']} rule #{rule['id']} would fire, suppressed")
            should_fire = False
        if not should_fire:
            continue

        label = rule.get("label") or "alert"
        subject = f"📈 {rule['symbol']} ({rule['exchange']}): {label}"
        body = "Triggered because:\n- " + "\n- ".join(reasons)
        body += f"\n\nLast price: {values.get('price')}"
        body += "\n\n(rules-based alert, not advice — verify before acting)"

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
