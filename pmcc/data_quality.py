from typing import Any, Dict, List

import pandas as pd

from pmcc.errors import DataQualityError


def build_option_data_quality(symbol: str, enriched: pd.DataFrame) -> Dict[str, Any]:
    total = int(len(enriched)) if enriched is not None else 0
    if enriched is None or enriched.empty:
        return {
            "status": "BLOCKED",
            "symbol": symbol,
            "contracts_checked": 0,
            "issues": ["No option contracts remained after merging Futu option chain and Greeks."],
            "yahoo_scope": "Yahoo Finance is price/event validation only; it is not used as an IV or Greeks fallback.",
        }

    delta_count = int(pd.to_numeric(enriched.get("delta"), errors="coerce").notna().sum()) if "delta" in enriched.columns else 0
    iv_count = (
        int(pd.to_numeric(enriched.get("implied_volatility"), errors="coerce").notna().sum())
        if "implied_volatility" in enriched.columns
        else 0
    )
    bid_count = int(pd.to_numeric(enriched.get("bid_price"), errors="coerce").notna().sum()) if "bid_price" in enriched.columns else 0
    ask_count = int(pd.to_numeric(enriched.get("ask_price"), errors="coerce").notna().sum()) if "ask_price" in enriched.columns else 0

    issues: List[str] = []
    warnings: List[str] = []
    if delta_count == 0:
        issues.append("Futu returned no usable option Delta values.")
    elif delta_count / total < 0.50:
        warnings.append(f"Only {delta_count}/{total} contracts have usable Delta values.")

    if iv_count == 0:
        issues.append("Futu returned no usable option implied volatility values.")
    elif iv_count / total < 0.50:
        warnings.append(f"Only {iv_count}/{total} contracts have usable implied volatility values.")

    if bid_count == 0 or ask_count == 0:
        warnings.append("Bid/ask data is missing, so liquidity checks may be incomplete.")

    status = "BLOCKED" if issues else ("WARN" if warnings else "OK")
    return {
        "status": status,
        "symbol": symbol,
        "contracts_checked": total,
        "delta_values": delta_count,
        "iv_values": iv_count,
        "bid_values": bid_count,
        "ask_values": ask_count,
        "issues": issues,
        "warnings": warnings,
        "yahoo_scope": "Yahoo Finance is price/event validation only; it is not used as an IV or Greeks fallback.",
    }


def require_option_data_quality(symbol: str, enriched: pd.DataFrame) -> Dict[str, Any]:
    quality = build_option_data_quality(symbol, enriched)
    if quality.get("status") == "BLOCKED":
        issues = "; ".join(quality.get("issues") or ["Unknown option data quality failure."])
        raise DataQualityError(
            f"DATA_QUALITY_BLOCKED for {symbol}: {issues} "
            "Check Futu OpenD login, US quote permission, option chain permission, and option Greeks availability. "
            "Yahoo Finance cannot replace Futu IV/Greeks for this strategy."
        )
    return quality
