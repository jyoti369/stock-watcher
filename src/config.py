"""Config + paths. Reads config.yaml if present, falls back to env vars, then defaults.

Secrets (SMTP password, Telegram token) should live in config.yaml which is
gitignored, or in environment variables. Nothing sensitive is committed.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "stocks.db"
CONFIG_PATH = ROOT / "config.yaml"

DATA_DIR.mkdir(exist_ok=True)


_DEFAULTS = {
    # how the watcher notifies you
    "alerts": {
        "channels": ["telegram", "email"],   # any of: telegram, email
        "cooldown_minutes": 360,             # don't re-fire the same rule within this window
    },
    "telegram": {
        "bot_token": "",
        "chat_id": "",
    },
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "password": "",       # gmail app password, not your login password
        "to": "",
    },
    "data": {
        "history_period": "10y",   # how much history to pull from yfinance
        "cache_minutes": 15,       # reuse fetched data within this window
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """Return merged config: defaults <- config.yaml <- selected env vars."""
    cfg = dict(_DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as fh:
            cfg = _deep_merge(cfg, yaml.safe_load(fh) or {})

    # env overrides for secrets (handy for launchd / CI without editing the yaml)
    env_map = {
        "STOCKWATCH_TG_TOKEN": ("telegram", "bot_token"),
        "STOCKWATCH_TG_CHAT": ("telegram", "chat_id"),
        "STOCKWATCH_SMTP_USER": ("email", "username"),
        "STOCKWATCH_SMTP_PASS": ("email", "password"),
        "STOCKWATCH_EMAIL_TO": ("email", "to"),
    }
    for env, (section, key) in env_map.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    return cfg


CONFIG = load()
