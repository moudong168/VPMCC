from datetime import date
from typing import Any, Optional

import pandas as pd


def safe_float(value: Any) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    numeric = safe_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def safe_text(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def parse_expiry(value: Any) -> Optional[date]:
    text = safe_text(value)
    if text is None:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def symbol_to_web_ticker(symbol: str) -> str:
    text = safe_text(symbol) or ""
    if "." in text:
        return text.split(".")[-1].upper()
    return text.upper()
