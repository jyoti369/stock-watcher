# Stock Watcher

A personal watcher + analysis dashboard for Indian equities (NSE / BSE), built on
free data. Three things in one small codebase:

1. **Dashboard** — one screen for your watchlist: live prices, 10-year price
   history with moving averages, trailing returns, and a fundamental scorecard.
2. **Alerts** — multi-condition rules (e.g. *P/E below 25 AND today down 3%*) that
   ping you on **Telegram and email** when they trigger. Runs in the background.
3. **Honest scoring** — a rules-based read of a company's fundamentals that says
   *OK / Mixed / Weak* **with the reasons**, plus historical context.

## The one thing it will never do

It will not tell you "buy this, hold X months, make Y profit." No tool can predict
future price reliably — the ones that claim to are selling something. This tool
describes *what is true about the past and present* (fundamentals, trends, ranges)
and leaves the decision to you. Every analysis carries that disclaimer.

## Setup

```bash
cd stock-watcher
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml   # then fill in Telegram / email if you want alerts
```

`config.yaml` and the local database are gitignored — nothing sensitive is committed.

## Use it

**Dashboard:**
```bash
./.venv/bin/streamlit run dashboard.py
```
Add symbols from the sidebar (just the ticker, e.g. `TCS`, `INFY`, `RELIANCE`).
Browse the Overview, drill into a stock under Stock analysis, and manage alert
rules under Alerts.

**Run the alert check once (manual):**
```bash
./.venv/bin/python -m src.watcher
```

**Run it automatically every 15 min (macOS launchd):**
```bash
cp scripts/com.stockwatcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockwatcher.plist
```
Edit the paths in the plist first if the project isn't at the default location.
(For always-on/no-machine alerts, GitHub Actions runs the watcher instead — see DEPLOY.md.)

## Alert types & reliability

- **Level vs edge rules** — a *level* rule fires every check while its conditions are
  true (throttled by cooldown); an *edge* rule fires once when they first *become*
  true (a crossing). Pick per rule in the Alerts tab.
- **Daily heartbeat** — `src/heartbeat.py` sends one digest a day (watchlist snapshot +
  a health line), so silence from the watcher means "nothing triggered", not "it broke".
- **Retries + fallback** — data fetches retry with backoff, and a symbol added under the
  wrong exchange auto-falls back to the other (NSE↔BSE).

## Tests
```bash
pip install -r requirements-dev.txt
pytest -q
```
Offline unit tests cover the metric, scoring, rule and projection maths; they also run
in CI on every push (`.github/workflows/tests.yml`).

## Where the data comes from

- **yfinance** (Yahoo) — price history and fundamentals. ~15 min delayed quotes.
- **nsepython** (NSE) — real-time NSE quotes, with a yfinance fallback.

Both are free and unofficial, so they occasionally rate-limit or return gaps; the
code handles that by degrading gracefully rather than crashing.

## Layout

```
dashboard.py          Streamlit UI
src/config.py         config + paths (reads config.yaml / env vars)
src/db.py             SQLite: watchlist, rules, alert history
src/datasource.py     yfinance + nsepython fetching, with caching
src/analysis.py       metrics + the honest fundamental scorer
src/alerts.py         Telegram + email senders
src/watcher.py        evaluates rules, fires alerts (run by launchd)
scripts/              launchd job template
data/                 SQLite db + logs (gitignored)
```
