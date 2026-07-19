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


def read_any_excel(file_obj, filename: str, password: str | None = None
                   ) -> tuple[pd.DataFrame | None, str | None]:
    """Read a broker CSV/XLSX robustly. Handles the two real-world traps:
    - files encrypted by the broker (tries Excel's default password, then the
      user-supplied one — typically your PAN)
    - Apple Numbers files renamed .xlsx (tells the user to export as CSV)
    Returns (df, error)."""
    import io as _io
    import zipfile

    raw = file_obj.read()
    name = filename.lower()

    if name.endswith(".csv"):
        try:
            return pd.read_csv(_io.BytesIO(raw)), None
        except Exception as e:
            return None, f"Couldn't read the CSV: {str(e)[:120]}"

    # Apple Numbers in disguise? (zip with iWork internals, not xl/)
    try:
        with zipfile.ZipFile(_io.BytesIO(raw)) as z:
            names = z.namelist()
            if any(n.startswith("Index/") and n.endswith(".iwa") for n in names):
                return None, ("This is an Apple **Numbers** file (renamed .xlsx). In Numbers: "
                              "File → Export To → **CSV**, then upload that.")
    except zipfile.BadZipFile:
        pass

    # plain xlsx first
    try:
        return pd.read_excel(_io.BytesIO(raw)), None
    except Exception:
        pass

    # encrypted? try Excel's silent default, then the user's password (PAN etc.)
    try:
        import msoffcrypto
        for pw in filter(None, ["VelvetSweatshop", password]):
            try:
                off = msoffcrypto.OfficeFile(_io.BytesIO(raw))
                off.load_key(password=pw)
                buf = _io.BytesIO()
                off.decrypt(buf)
                buf.seek(0)
                return pd.read_excel(buf), None
            except Exception:
                continue
        return None, ("This file is **password-locked** by the broker (usually your PAN, "
                      "in CAPITALS). Enter it in the password box and hit Read file again — "
                      "it's used only to open the file, never stored.")
    except ImportError:
        return None, "Locked file support missing (msoffcrypto-tool not installed)."


def _sniff_header(df: pd.DataFrame) -> pd.DataFrame:
    """Broker sheets often stack title/logo rows above the real header. If the
    current columns don't look like a header, hunt one in the first 10 rows."""
    headers = {_norm_header(c): c for c in df.columns}
    if _find_col(headers, _SYMBOL_KEYS) and _find_col(headers, _QTY_KEYS):
        return df
    for i in range(min(10, len(df))):
        row = [str(v) for v in df.iloc[i].tolist()]
        h = {_norm_header(v): v for v in row}
        if _find_col(h, _SYMBOL_KEYS) and _find_col(h, _QTY_KEYS) and _find_col(h, _PRICE_KEYS):
            out = df.iloc[i + 1:].copy()
            out.columns = row
            return out
    return df


def parse_table(df: pd.DataFrame) -> tuple[list[dict[str, Any]], str | None]:
    """Broker CSV/XLSX -> candidate rows. Returns (rows, error)."""
    if df is None or df.empty:
        return [], "The file is empty."
    df = _sniff_header(df)
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
