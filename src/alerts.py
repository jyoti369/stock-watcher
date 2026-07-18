"""Notification channels: Telegram and email. Both are optional and degrade
gracefully — if a channel isn't configured it's skipped, not fatal.
"""
from __future__ import annotations

import smtplib
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
        return r.ok
    except Exception:
        return False


def send_email(subject: str, body: str) -> bool:
    em = CONFIG["email"]
    if not em.get("username") or not em.get("password") or not em.get("to"):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = em["username"]
        msg["To"] = em["to"]
        with smtplib.SMTP(em["smtp_host"], int(em["smtp_port"]), timeout=20) as s:
            s.starttls()
            s.login(em["username"], em["password"])
            s.send_message(msg)
        return True
    except Exception:
        return False


def dispatch(subject: str, body: str, channels: list[str] | None = None) -> list[str]:
    """Send to the requested channels (default: configured ones). Returns the
    channels that actually succeeded."""
    channels = channels or CONFIG["alerts"]["channels"]
    sent = []
    if "telegram" in channels and send_telegram(f"<b>{subject}</b>\n{body}"):
        sent.append("telegram")
    if "email" in channels and send_email(subject, body):
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
