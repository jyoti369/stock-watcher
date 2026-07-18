"""Web-grounded AI insight layer.

Design note: rather than depend on each LLM provider's built-in web-search quota
(Gemini's grounding tier is tiny; OpenAI's web tool needs paid credits), we do the
retrieval ourselves — pull real, recent headlines for free (Google News RSS +
yfinance) — and hand those to the model to summarize. So every claim is anchored
to articles we actually fetched, and the sources shown are those real URLs.

Money-tool guardrail: the prompt forbids buy/sell calls, target prices and
predictions. It asks only for a factual, sourced read of the current situation,
catalysts and risks — and to say so when the news is thin.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from . import datasource
from .config import CONFIG

_RULES = (
    "You are a careful equity-research assistant for Indian stocks. Using ONLY the "
    "headlines provided below, write under 160 words of plain prose covering: "
    "(1) what's driving the stock recently, (2) upcoming catalysts, (3) key risks. "
    "Ground each point in the headlines; if they don't support a point, don't make it. "
    "No buy/sell advice, no target prices, no predictions. If the news is thin, say so."
)


def available() -> dict[str, bool]:
    ai = CONFIG["ai"]
    return {"gemini": bool(ai.get("gemini_api_key")), "openai": bool(ai.get("openai_api_key"))}


# ---- free retrieval ------------------------------------------------------

def _google_news(query: str, limit: int = 6) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok:
            return []
        root = ET.fromstring(r.content)
        out = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            date = (it.findtext("pubDate") or "").strip()[:16]
            if title and link:
                out.append({"title": title, "url": link, "date": date})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def gather_news(symbol: str, name: str | None = None, exchange: str = "NSE") -> list[dict]:
    """Real recent headlines from Google News (India) + yfinance, deduped."""
    query = f"{name or symbol} share NSE" if name else f"{symbol} stock NSE"
    items = _google_news(query)
    for n in datasource.get_news(symbol, exchange, limit=4):
        items.append({"title": n["title"], "url": n.get("url", ""), "date": n.get("date", "")})
    seen, deduped = set(), []
    for it in items:
        k = it["title"].lower()[:60]
        if k not in seen and it["title"]:
            seen.add(k)
            deduped.append(it)
    return deduped[:7]


# ---- model synthesis (plain calls, no provider web-search needed) ---------

def _prompt(symbol: str, context: str, news: list[dict]) -> str:
    lines = "\n".join(f"- {n['title']} ({n['date']})" for n in news)
    return (f"{_RULES}\n\nStock: {symbol} (NSE).\n"
            f"Our computed figures (context only, don't just restate): {context}\n\n"
            f"Recent headlines:\n{lines}\n\nWrite the grounded summary now.")


def _gemini(prompt: str) -> str:
    ai = CONFIG["ai"]
    model = ai.get("model_gemini", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={ai['gemini_api_key']}"
    r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=45)
    if not r.ok:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:160]}")
    cand = (r.json().get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()


def _openai(prompt: str) -> str:
    ai = CONFIG["ai"]
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {ai['openai_api_key']}"},
        json={"model": ai.get("model_openai", "gpt-4o-mini"),
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60)
    if not r.ok:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:160]}")
    return r.json()["choices"][0]["message"]["content"].strip()


def generate(symbol: str, context: str, name: str | None = None,
             engine: str | None = None) -> dict[str, Any] | None:
    """Return {text, sources, engine} or {error}. None if no key at all."""
    avail = available()
    if not (avail["gemini"] or avail["openai"]):
        return None
    engine = engine or CONFIG["ai"].get("provider", "gemini")
    if engine == "openai" and not avail["openai"]:
        engine = "gemini"
    if engine == "gemini" and not avail["gemini"]:
        engine = "openai"

    news = gather_news(symbol, name)
    if not news:
        return {"error": "No recent headlines found to ground an insight."}
    prompt = _prompt(symbol, context, news)
    try:
        text = _openai(prompt) if engine == "openai" else _gemini(prompt)
    except Exception as e:
        return {"error": str(e)[:200]}
    label = "OpenAI" if engine == "openai" else "Gemini"
    return {"text": text, "sources": news, "engine": f"{label} · grounded in fetched news"}
