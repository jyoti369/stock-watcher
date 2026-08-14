"""What the app calls itself.

It started as a stock watcher and is now closer to a personal assistant — it
holds the portfolio, the money plan, the IPO screener, the advice ledger,
reminders and the shopping tracker, and mails you the parts that need a
decision. "Stock Watcher" undersold that.

The name comes from the Telegram bot already carrying it (@niki25_bot), so the
app and the thing that pings your phone are finally the same character. All of
it lives here: change NAME and every screen, subject line and mail header
follows.
"""

NAME = "Niki"
TAGLINE = "your money, your holdings, your buying decisions"

# what shows in the browser tab / phone home screen
PAGE_TITLE = NAME
ICON = "🪄"

# mail subject prefixes — kept short so a phone shows the useful half
DIGEST_SUBJECT = f"📊 {NAME}"
BRIEF_SUBJECT = f"🌅 {NAME}"
ALERT_SUBJECT = "🔔"

DIGEST_TITLE = f"{NAME} · daily digest"
BRIEF_TITLE = f"{NAME} · midday brief"
