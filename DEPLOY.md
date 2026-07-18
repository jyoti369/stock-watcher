# Deploying Stock Watcher

Your setup: **alerts run 24/7 on GitHub Actions** (no machine of yours stays on,
no card), and you **run the dashboard locally** when you want to browse or change
your watchlist. This is the primary path below. A fully-hosted container option
(Fly.io / any Docker host) is kept at the end as an alternative.

---

## Primary: 24/7 alerts on GitHub Actions + local dashboard

### How it fits together
- The **local dashboard** is your control panel. When you add a stock or an alert
  rule, it writes `state/watchlist.json` and `state/rules.json`. Hit **“⬆️ Sync
  watchlist/rules to GitHub”** in the sidebar (or `git push`) to publish them.
- The **GitHub Action** (`.github/workflows/alerts.yml`) wakes up every 30 min
  during NSE market hours, reads that state, checks your rules, and messages you
  on Telegram / email. It writes cooldown state back to `state/alert_state.json`
  so you never get spammed. No computer of yours is involved.

### One-time setup

1. **Create a Telegram bot** (for instant phone alerts)
   - In Telegram, message **@BotFather** → `/newbot` → it gives you a **token**.
   - Message your new bot once (say “hi”), then open
     `https://api.telegram.org/bot<token>/getUpdates` in a browser and copy the
     `chat.id` number.

2. **Create a Gmail app password** (for email alerts)
   - Google Account → Security → 2-Step Verification → **App passwords** → generate
     one for “Mail”. Use that 16-char password (not your login password).

3. **Add them as repo secrets** — in the GitHub repo:
   Settings → Secrets and variables → Actions → **New repository secret**. Add the
   ones you want (Telegram, email, or both):
   ```
   STOCKWATCH_TG_TOKEN     = 123456:ABC...
   STOCKWATCH_TG_CHAT      = 987654321
   STOCKWATCH_SMTP_USER    = you@gmail.com
   STOCKWATCH_SMTP_PASS    = your-gmail-app-password
   STOCKWATCH_EMAIL_TO     = you@gmail.com
   ```
   Or from the terminal:
   ```bash
   gh secret set STOCKWATCH_TG_TOKEN
   gh secret set STOCKWATCH_TG_CHAT
   # …and so on (it prompts for each value; nothing is stored in the repo)
   ```

4. **Test it** — GitHub repo → **Actions** tab → “alert-watcher” → **Run workflow**.
   Watch the run; if a rule’s conditions are met you’ll get the alert. It also runs
   automatically every 30 min in market hours after this.

### Making phone/cloud edits permanent (STOCKWATCH_GH_TOKEN)

The hosted app can't `git push`, so by default changes made there (holdings,
rules, scans) only live until the container restarts. Fix: give it a token so it
saves through the GitHub API.

1. GitHub → Settings → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → *Generate new token*.
2. Repository access: **Only select repositories** → `stock-watcher`.
   Permissions: **Contents → Read and write**. Expiry: up to you (set a reminder).
3. Copy the `github_pat_…` value and add one line to the Streamlit Cloud
   secrets box:
   ```toml
   STOCKWATCH_GH_TOKEN = "github_pat_..."
   ```
Sidebar shows “🟢 Changes auto-save to GitHub” when it's active. Scope stays
limited to this one repo, so the blast radius if it ever leaks is small — but
rotate it like any secret.

### Managing your watchlist / rules
Run the dashboard locally, make changes, then click **Sync to GitHub**:
```bash
./.venv/bin/streamlit run dashboard.py
```
The Action picks up the new state on its next run.

### Cost
Scheduling only during market hours (weekdays, ~03:00–10:00 UTC) keeps this well
inside the free GitHub Actions minutes for a private repo (~2000/month).

---

## Alternative: fully-hosted container (Fly.io / Docker)

If you later get an international card, the same code runs as one always-on
container serving **both** the dashboard and the watcher.

```bash
docker build -t stock-watcher .
docker run --rm -p 8080:8080 -v "$PWD/data:/app/data" \
  -e STOCKWATCH_TG_TOKEN=... -e STOCKWATCH_TG_CHAT=... stock-watcher
# http://localhost:8080
```

For Fly.io: `brew install flyctl && fly auth login`, then `fly launch` (uses the
included `fly.toml` + `Dockerfile`), `fly volumes create stockdata`, set secrets
with `fly secrets set …`, and `fly deploy`. Note Fly requires a card even on the
free allowance. Any Docker host (Railway, Koyeb, Render) works the same way.

## Data-source note
From a cloud/GitHub IP, NSE’s live-quote endpoint is often blocked, so quotes fall
back to yfinance (~15-min delayed). Fundamentals, scoring, suggestions and alerts
all work fine; only the real-time tick may be delayed.
