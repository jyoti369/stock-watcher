# Deploying Stock Watcher (always-on)

The goal: one small always-on container that runs **both** the dashboard and the
24/7 alert watcher, with a persistent database and a URL you can open anywhere.
The `Dockerfile` is host-agnostic, so any container host works — instructions
below are for **Fly.io** (good free-ish tier, Singapore region close to India).

## What runs where

- `entrypoint.sh` starts `src.scheduler` (the alert watcher loop) in the background
  and `dashboard.py` (Streamlit) in the foreground.
- SQLite lives on a mounted volume at `/app/data` so your watchlist, rules and
  alert history survive redeploys.
- Secrets (Telegram token, Gmail app password) are set as **environment variables /
  Fly secrets**, never committed.

## Fly.io

1. Install the CLI and log in (needs a Fly account; a card is required even for the
   free allowance):
   ```bash
   brew install flyctl
   fly auth login
   ```

2. From the project root, pick a unique app name and create the app + volume:
   ```bash
   fly launch --no-deploy --name stock-watcher-jyoti --region sin
   fly volumes create stockdata --region sin --size 1
   ```
   (`fly launch` will notice the existing `fly.toml` and `Dockerfile`.)

3. Set your secrets (only the channels you use):
   ```bash
   fly secrets set \
     STOCKWATCH_TG_TOKEN="123456:abc..." \
     STOCKWATCH_TG_CHAT="your_chat_id" \
     STOCKWATCH_SMTP_USER="you@gmail.com" \
     STOCKWATCH_SMTP_PASS="gmail_app_password" \
     STOCKWATCH_EMAIL_TO="you@gmail.com"
   ```

4. Deploy:
   ```bash
   fly deploy
   ```
   `fly open` opens the dashboard URL. `fly logs` tails both the dashboard and the
   watcher (you'll see `[scheduler]` lines every 15 min).

### Keeping it private (recommended for a personal tool)
Fly apps are reachable by URL but not indexed. To require login, put it behind
[Fly's `tls`/OIDC or a simple auth proxy], or add a password gate in Streamlit
(`st.text_input(type="password")` check) — ask and I'll wire one in.

## Test the container locally first (Docker)

```bash
docker build -t stock-watcher .
docker run --rm -p 8080:8080 \
  -e STOCKWATCH_TG_TOKEN=... -e STOCKWATCH_TG_CHAT=... \
  -v "$PWD/data:/app/data" \
  stock-watcher
# open http://localhost:8080
```

## Other hosts

The same `Dockerfile` deploys to **Railway**, **Koyeb**, or **Render** — create a
service from the repo, add the `STOCKWATCH_*` env vars, attach a volume at
`/app/data`, and expose port 8080. On hosts whose free tier sleeps on idle, the
24/7 watcher won't fire while asleep — use a plan that stays running.

## Data-source note

From a cloud IP, NSE's live-quote endpoint (nsepython) is often blocked, so quotes
fall back to yfinance (~15-min delayed). Fundamentals, scoring, suggestions and
alerts all work fine from the cloud; only the real-time tick may be delayed.
