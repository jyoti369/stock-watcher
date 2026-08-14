"""Notification channels: Telegram and email. Both are optional and degrade
gracefully — if a channel isn't configured it's skipped, not fatal.
"""
from __future__ import annotations

import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import CONFIG


def send_telegram(text: str) -> bool:
    tg = CONFIG["telegram"]
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            # printed, not swallowed: a rejected message used to vanish silently
            print(f"[telegram] {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"[telegram] send failed: {str(e)[:200]}")
        return False


def send_email(subject: str, body: str, html_body: str | None = None) -> bool:
    """Sends multipart/alternative when an HTML body is given: clients that
    render HTML show the laid-out version, anything else falls back to the same
    text Telegram gets. Order matters — the HTML part must come last."""
    em = CONFIG["email"]
    if not em.get("username") or not em.get("password") or not em.get("to"):
        return False
    try:
        if html_body:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = em["username"]
        msg["To"] = em["to"]
        with smtplib.SMTP(em["smtp_host"], int(em["smtp_port"]), timeout=20) as s:
            s.starttls()
            s.login(em["username"], em["password"])
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[email] send failed: {str(e)[:200]}")
        return False


def dispatch(subject: str, body: str, channels: list[str] | None = None,
             html_body: str | None = None) -> list[str]:
    """Send to the requested channels (default: configured ones). Returns the
    channels that actually succeeded. `body` is the plain-text version — it
    always goes to Telegram, which has no HTML layout worth the name."""
    channels = channels or CONFIG["alerts"]["channels"]
    sent = []
    # Telegram parses the message as HTML, so anything in the text that looks
    # like markup has to be escaped first — an unescaped "&" in a company name
    # ("Q&T Foods") is enough for Telegram to reject the whole message.
    if "telegram" in channels and send_telegram(
            f"<b>{html.escape(subject)}</b>\n{html.escape(body)}"):
        sent.append("telegram")
    if "email" in channels and send_email(subject, body, html_body):
        sent.append("email")
    return sent


def channel_status() -> dict[str, bool]:
    """For the dashboard: which channels are actually configured."""
    tg = CONFIG["telegram"]
    em = CONFIG["email"]
    return {
        "telegram": bool(tg.get("bot_token") and tg.get("chat_id")),
        "email": bool(em.get("username") and em.get("password") and em.get("to")),
    }
