"""
CBOE delayed quotes API - free options chain + Greeks data.

Uses CBOE's public delayed quotes API (no API key required).
Data is delayed by approximately 15-20 minutes for US equity options.

Source: https://www.cboe.com/ - https://cdn.cboe.com/api/global/delayed_quotes/
"""

import json
import re
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from pmcc.utils import safe_float, safe_int

BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# CBOE option code format: TICKER + YYMMDD + C/P + strike*1000 (zero-padded 8 digits)
# Example: NVDA260626C00050000 -> NVDA, 2026-06-26, Call, $50.00
CBOE_CODE_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_cboe_code(code: str) -> Dict[str, Any]:
    """Parse a CBOE option code into its components.

    Returns dict with: underlying, expiry (date or None), option_type (call/put), strike (float or None).
    """
    match = CBOE_CODE_RE.match(code)
    if not match:
        return {"underlying": "", "expiry": None, "option_type": "", "strike": None}
    underlying = match.group(1)
    expiry_str = match.group(2)
    opt_type = "call" if match.group(3) == "C" else "put"
    strike = round(int(match.group(4)) / 1000.0, 2)
    try:
        expiry = datetime.strptime(expiry_str, "%y%m%d").date()
    except ValueError:
        expiry = None
    return {"underlying": underlying, "expiry": expiry, "option_type": opt_type, "strike": strike}


def _cboe_ticker(symbol: str) -> str:
    """Strip Futu-style prefix (e.g. US.NVDA -> NVDA) from ticker symbol."""
    return symbol.upper().split(".")[-1]


def fetch_cboe_data(symbol: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Fetch the full option chain (all expiries, calls + puts) with Greeks from CBOE.

    This single request replaces Futu's get_option_chain + get_greeks calls.

    Returns (DataFrame, metadata_dict).

    DataFrame columns (compatible with enrich_options() in futu_option_decision.py):
      code           - str, CBOE option symbol (e.g. NVDA260626C00050000)
      strike_price   - float, the strike price
      strike_time    - str, expiry date in YYYY-MM-DD format
      option_type    - str, "CALL" or "PUT"
      implied_volatility - float or NaN
      delta, gamma, theta, vega, rho - float or NaN
      bid_price, ask_price, last_price - float or NaN
      open_interest, volume - int or 0
      underlying_price - float

    metadata_dict:
      current_price  - float, underlying stock price
      iv30           - float or None, 30-day implied volatility index
      open, high, low, close - float or None
      volume         - int or None
      symbol         - str

    Raises RuntimeError on HTTP or parse failure.
    """
    url = f"{BASE_URL}/options/{_cboe_ticker(symbol)}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CBOE API request failed for {symbol}: {exc}") from exc

    data = raw.get("data") or {}
    options_list = data.get("options") or []
    if not options_list:
        raise RuntimeError(f"CBOE returned no options data for {symbol}")

    current_price = safe_float(data.get("current_price"))
    metadata = {
        "symbol": data.get("symbol") or symbol.upper(),
        "current_price": current_price,
        "iv30": safe_float(data.get("iv30")),
        "open": safe_float(data.get("open")),
        "high": safe_float(data.get("high")),
        "low": safe_float(data.get("low")),
        "close": safe_float(data.get("close")),
        "volume": safe_int(data.get("volume")),
    }

    today = datetime.today().date()
    rows = []
    for opt in options_list:
        code = opt.get("option", "")
        parsed = parse_cboe_code(code)
        strike = parsed["strike"]
        expiry = parsed["expiry"]
        if strike is None or expiry is None:
            continue
        dte = (expiry - today).days
        # CBOE reports IV as a decimal fraction (e.g. 0.312); the rest of the
        # program (Futu chain, IV history, IV Rank) uses percentage points.
        cboe_iv = safe_float(opt.get("iv"))
        rows.append({
            "code": code,
            "strike_price": strike,
            "strike_time": expiry.isoformat(),
            "option_type": parsed["option_type"].upper(),
            "implied_volatility": round(cboe_iv * 100, 4) if cboe_iv is not None else None,
            "delta": safe_float(opt.get("delta")),
            "gamma": safe_float(opt.get("gamma")),
            "theta": safe_float(opt.get("theta")),
            "vega": safe_float(opt.get("vega")),
            "rho": safe_float(opt.get("rho")),
            "bid_price": safe_float(opt.get("bid")),
            "ask_price": safe_float(opt.get("ask")),
            "last_price": safe_float(opt.get("last_trade_price")),
            "open_interest": safe_int(opt.get("open_interest")),
            "volume": safe_int(opt.get("volume")),
            "days_to_expiry": dte,
            "underlying_price": current_price,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"CBOE option chain for {symbol} produced zero valid rows")

    for col in ["strike_price", "implied_volatility", "delta", "gamma", "theta", "vega", "rho",
                "bid_price", "ask_price", "last_price", "underlying_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["open_interest", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    if "days_to_expiry" in df.columns:
        df["days_to_expiry"] = pd.to_numeric(df["days_to_expiry"], errors="coerce").fillna(0).astype(int)

    return df.sort_values(by=["strike_price"]).reset_index(drop=True), metadata


def fetch_cboe_quote_only(symbol: str) -> Dict[str, Any]:
    """Fetch just the stock quote + iv30 from CBOE (lighter than the full option chain).

    Returns {symbol, current_price, iv30, open, high, low, close, volume, bid, ask, bid_size, ask_size}
    """
    url = f"{BASE_URL}/quotes/{_cboe_ticker(symbol)}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CBOE quote request failed for {symbol}: {exc}") from exc

    d = raw.get("data") or {}
    return {
        "symbol": d.get("symbol") or symbol.upper(),
        "current_price": safe_float(d.get("current_price")),
        "iv30": safe_float(d.get("iv30")),
        "bid": safe_float(d.get("bid")),
        "ask": safe_float(d.get("ask")),
        "open": safe_float(d.get("open")),
        "high": safe_float(d.get("high")),
        "low": safe_float(d.get("low")),
        "close": safe_float(d.get("close")),
        "volume": safe_int(d.get("volume")),
        "bid_size": safe_int(d.get("bid_size")),
        "ask_size": safe_int(d.get("ask_size")),
    }
