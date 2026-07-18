#!/usr/bin/env bash
# Start the alert watcher in the background, then the dashboard in the foreground
# (foreground process keeps the container alive).
set -e

echo "starting alert scheduler…"
python -m src.scheduler &

echo "starting dashboard on :${PORT:-8080}…"
exec streamlit run dashboard.py \
    --server.port "${PORT:-8080}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
