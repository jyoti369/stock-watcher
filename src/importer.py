"""Import holdings from broker exports (Angel One etc.) — file or pasted text.

Three parsers, in order of trust:
  parse_table(df)   — a downloaded holdings CSV/XLSX; maps whatever the broker
                      called its columns onto (symbol, qty, buy_price).
  parse_text(text)  — rows pasted from the app/statement; regex heuristics.
  parse_with_ai(..) — Gemini fallback for messy pastes; output is validated and
                      ALWAYS shown in an editable preview before anything saves,
                      because a misread quantity in a money tool is unacceptable.

Nothing here writes to the database — parsers return candidate rows; the
dashboard previews them and only saves on explicit confirm.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

# header keywords -> our field, tried in order (first match wins per field)
_SYMBOL_KEYS = ["tradingsymbol", "symbol", "scrip", "instrument", "stockname", "stock", "name"]
_QTY_KEYS = ["netqty", "totalqty", "quantityavailable", "quantity", "qty", "shares", "units"]
_PRICE_KEYS = ["avgbuyprice", "buyavgprice", "avgcostprice", "averageprice", "avgprice",
               "buyavg", "avgcost", "buyprice", "costprice", "avgrate"]

_SUFFIXES = ("-EQ", "-BE", "-BZ", "-SM", "-ST")


def clean_symbol(raw: str) -> str:
    """'INFY-EQ' -> 'INFY', 'NSE:TCS' -> 'TCS'. Keeps &/- inside names (M&M, BAJAJ-AUTO)."""
    s = str(raw).strip().upper()
    s = re.sub(r"^(NSE|BSE)\s*[:>]\s*", "", s)
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z]", "", str(h).lower())


def _find_col(headers: dict[str, str], keys: list[str]) -> str | None:
    for k in keys:
        for norm, original in headers.items():
            if k in norm:
                return original
    return None


def _to_num(v) -> float | None:
    """'1,234.50' / '₹1234.5' / 1234.5 -> float. None if not numeric."""
    if isinstance(v, (int, float)) and not pd.isna(v):
        return float(v)
    s = re.sub(r"[₹,\s]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def parse_table(df: pd.DataFrame) -> tuple[list[dict[str, Any]], str | None]:
    """Broker CSV/XLSX -> candidate rows. Returns (rows, error)."""
    if df is None or df.empty:
        return [], "The file is empty."
    headers = {_norm_header(c): c for c in df.columns}
    sym_col = _find_col(headers, _SYMBOL_KEYS)
    qty_col = _find_col(headers, _QTY_KEYS)
    price_col = _find_col(headers, _PRICE_KEYS)
    if not (sym_col and qty_col and price_col):
        missing = [n for n, c in [("symbol", sym_col), ("quantity", qty_col),
                                  ("avg buy price", price_col)] if not c]
        return [], f"Couldn't find column(s) for: {', '.join(missing)}. Columns seen: {list(df.columns)}"
    rows = []
    for _, r in df.iterrows():
        sym = clean_symbol(r[sym_col])
        qty, price = _to_num(r[qty_col]), _to_num(r[price_col])
        if sym and qty and price and qty > 0 and price > 0:
            rows.append({"symbol": sym, "qty": qty, "buy_price": round(price, 2)})
    return rows, (None if rows else "No valid holding rows found in the file.")


_LINE_RE = re.compile(
    r"^\s*(?P<sym>[A-Za-z][A-Za-z0-9&\-\.]{1,25}?)(?:-EQ|-BE)?\s+"
    r"(?P<a>[\d,]+(?:\.\d+)?)\s+(?:[xX@]\s*)?(?P<b>[\d,]+(?:\.\d+)?)\s*$")


def parse_text(text: str) -> list[dict[str, Any]]:
    """Pasted 'SYMBOL qty price' style lines -> candidate rows. Heuristic: of the
    two numbers, the whole-number one is qty; ties go (qty, price) in order."""
    rows = []
    for line in text.strip().splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        a, b = _to_num(m["a"]), _to_num(m["b"])
        if a is None or b is None:
            continue
        if a != int(a) and b == int(b):
            qty, price = b, a                     # price came first — swap
        else:
            qty, price = a, b
        sym = clean_symbol(m["sym"])
        if sym and qty > 0 and price > 0:
            rows.append({"symbol": sym, "qty": qty, "buy_price": round(price, 2)})
    return rows


def parse_with_ai(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Gemini fallback for messy pastes. Returns (rows, error)."""
    from . import ai_insights
    if not ai_insights.available().get("gemini"):
        return [], "AI parsing needs the Gemini key."
    prompt = (
        "Extract stock holdings from the text below (an Indian broker portfolio). "
        "Return ONLY a JSON array, no prose, each item: "
        '{"symbol": "<NSE ticker, uppercase, no -EQ suffix>", "qty": <number>, '
        '"buy_price": <average buy price in rupees>}. '
        "Skip totals/headers/anything that is not a holding.\n\n" + text[:6000])
    try:
        raw = ai_insights._gemini(prompt)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        rows = []
        for d in data if isinstance(data, list) else []:
            sym = clean_symbol(d.get("symbol", ""))
            qty, price = _to_num(d.get("qty")), _to_num(d.get("buy_price"))
            if sym and qty and price and qty > 0 and price > 0:
                rows.append({"symbol": sym, "qty": qty, "buy_price": round(price, 2)})
        return rows, (None if rows else "AI couldn't find holdings in that text.")
    except Exception as e:
        return [], f"AI parse failed: {str(e)[:120]}"
