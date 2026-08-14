"""What the app calls itself.

It started as a stock watcher and is now closer to a personal assistant — it
holds the portfolio, the money plan, the IPO screener, the advice ledger,
reminders and the shopping tracker, notices things across all of them, and
mails you the parts that need a decision. "Stock Watcher" undersold that.

Named for the obvious model: an assistant that keeps the books, watches the
feeds, and says the useful thing without being asked. Every screen, subject
line and mail header reads from here, so renaming is one edit.
"""

NAME = "Jarvis"
TAGLINE = "money, holdings and buying decisions — at your service"

# what shows in the browser tab / phone home screen
PAGE_TITLE = NAME
ICON = "🤖"

# mail subject prefixes — kept short so a phone shows the useful half
DIGEST_SUBJECT = f"📊 {NAME}"
BRIEF_SUBJECT = f"🌅 {NAME}"
ALERT_SUBJECT = "🔔"

DIGEST_TITLE = f"{NAME} · daily digest"
BRIEF_TITLE = f"{NAME} · midday brief"
