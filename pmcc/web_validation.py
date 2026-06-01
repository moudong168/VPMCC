import html
import json
import re
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Dict, List, Optional

from pmcc.utils import parse_expiry, safe_float, safe_int, safe_text, symbol_to_web_ticker


def http_get_text(url: str, timeout: float = 5.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 PMCC-Validator/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def visible_page_text(page: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def build_earnings_source_record(
    name: str,
    source: str,
    url: str,
    ticker: str,
    matched_text: Optional[str],
    parsed_date: Optional[date],
    time_of_day: Optional[str] = None,
    status_text: Optional[str] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "name": name,
        "source": source,
        "url": url,
        "status": "ERROR",
        "ticker": ticker.upper(),
    }
    if parsed_date is None:
        record["error"] = "earnings date not found"
        return record
    record.update(
        {
            "status": "OK",
            "next_earnings_date": parsed_date.isoformat(),
            "days_to_earnings": (parsed_date - date.today()).days,
            "matched_text": matched_text,
            "time_of_day": time_of_day,
            "confirmation_status": status_text,
        }
    )
    return record


def fetch_yahoo_finance_quote(symbol: str) -> Dict[str, Any]:
    ticker = symbol_to_web_ticker(symbol)
    human_url = f"https://finance.yahoo.com/quote/{ticker}/"
    api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    record: Dict[str, Any] = {
        "name": "Yahoo Finance",
        "source": "yahoo_finance",
        "url": human_url,
        "status": "ERROR",
    }
    try:
        payload = json.loads(http_get_text(api_url))
        results = ((payload.get("chart") or {}).get("result") or [])
        meta = (results[0].get("meta") or {}) if results else {}
        price = safe_float(meta.get("regularMarketPrice"))
        if price is None:
            html_text_payload = http_get_text(human_url)
            match = re.search(r'"regularMarketPrice"\s*:\s*\{[^{}]*"raw"\s*:\s*([0-9.]+)', html_text_payload)
            price = safe_float(match.group(1)) if match else None
        if price is None:
            record["error"] = "regularMarketPrice not found"
            return record
        record.update(
            {
                "status": "OK",
                "ticker": ticker,
                "price": round(price, 4),
                "currency": meta.get("currency"),
                "market_time": meta.get("regularMarketTime"),
                "api_url": api_url,
            }
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        record["error"] = str(exc)
    return record


def fetch_next_earnings_date(symbol: str) -> Dict[str, Any]:
    ticker = symbol_to_web_ticker(symbol).lower()
    url = f"https://nextearningsdate.com/{ticker}.html"
    record: Dict[str, Any] = {
        "name": "NextEarningsDate",
        "source": "next_earnings_date",
        "url": url,
        "status": "ERROR",
    }
    try:
        page = http_get_text(url)
        patterns = [
            rf"next projected earnings date for\s+{re.escape(ticker)}\s+is\s+(\d{{1,2}}/\d{{1,2}}/20\d{{2}})",
            r"next projected earnings date[^.]{0,160}?\s+is\s+(\d{1,2}/\d{1,2}/20\d{2})",
            r"next earnings report will[^.]{0,160}?on\s+(\d{1,2}/\d{1,2}/20\d{2})",
            r"next earnings date[^.]{0,160}?([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+20\d{2})",
        ]
        parsed_date = None
        matched_text = None
        for pattern in patterns:
            match = re.search(pattern, page, flags=re.IGNORECASE)
            if not match:
                continue
            matched_text = match.group(1)
            parsed_date = parse_expiry(matched_text.replace(".", ""))
            if parsed_date is not None:
                break
        if parsed_date is None:
            record["error"] = "earnings date not found"
            return record
        days_to_earnings = (parsed_date - date.today()).days
        record.update(
            {
                "status": "OK",
                "ticker": ticker.upper(),
                "next_earnings_date": parsed_date.isoformat(),
                "matched_text": matched_text,
                "days_to_earnings": days_to_earnings,
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        record["error"] = str(exc)
    return record


def wallstreethorizon_slug(ticker: str) -> str:
    mapping = {
        "AAPL": "apple",
        "AMD": "amd",
        "AMZN": "amazon",
        "AVGO": "broadcom",
        "GOOG": "alphabet",
        "GOOGL": "alphabet",
        "META": "meta",
        "MSFT": "microsoft",
        "NVDA": "nvidia",
        "TSLA": "tesla",
    }
    return mapping.get(ticker.upper(), ticker.lower())


def fetch_wallstreethorizon_earnings_date(symbol: str) -> Dict[str, Any]:
    ticker = symbol_to_web_ticker(symbol).upper()
    slug = wallstreethorizon_slug(ticker)
    url = f"https://www.wallstreethorizon.com/{slug}-earnings-calendar"
    try:
        text = visible_page_text(http_get_text(url))
        patterns = [
            r"next earnings date is\s+([A-Z]+)\s+for\s+(?:[A-Za-z]+\s+)?(\d{1,2}/\d{1,2}/20\d{2})\s+((?:Before|After)\s+Market)",
            r"(\d{1,2}/\d{1,2}/20\d{2})\s+Q\d+\s+20\d{2}\s+((?:Before|After)\s+Market)\s+[A-Za-z]{3}\s+([A-Z]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            groups = match.groups()
            if re.match(r"\d", groups[0]):
                date_text, time_text, status_text = groups[0], groups[1], groups[2]
            else:
                status_text, date_text, time_text = groups[0], groups[1], groups[2]
            parsed_date = parse_expiry(date_text)
            return build_earnings_source_record(
                "Wall Street Horizon",
                "wall_street_horizon_earnings",
                url,
                ticker,
                " ".join(groups),
                parsed_date,
                safe_text(time_text),
                safe_text(status_text),
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        record = build_earnings_source_record("Wall Street Horizon", "wall_street_horizon_earnings", url, ticker, None, None)
        record["error"] = str(exc)
        return record
    return build_earnings_source_record("Wall Street Horizon", "wall_street_horizon_earnings", url, ticker, None, None)


def choose_primary_earnings_source(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    usable = [
        record
        for record in records
        if record.get("status") == "OK"
        and safe_int(record.get("days_to_earnings")) is not None
        and safe_int(record.get("days_to_earnings")) >= 0
    ]
    if not usable:
        return None
    usable.sort(
        key=lambda item: (
            safe_int(item.get("days_to_earnings")) or 999999,
            0 if str(item.get("confirmation_status") or "").lower() in {"confirmed", "confirmed)"} else 1,
        )
    )
    return usable[0]


def build_market_data_validation(symbol: str, futu_price: Optional[float], enabled: bool = True) -> Dict[str, Any]:
    validation: Dict[str, Any] = {
        "enabled": enabled,
        "primary_source": "Futu OpenD",
        "sources": [],
        "price_check": {"status": "SKIPPED"},
        "earnings_check": {"status": "SKIPPED"},
    }
    if not enabled:
        return validation

    yahoo = fetch_yahoo_finance_quote(symbol)
    earnings_sources = [
        fetch_next_earnings_date(symbol),
        fetch_wallstreethorizon_earnings_date(symbol),
    ]
    earnings = choose_primary_earnings_source(earnings_sources)
    validation["sources"] = [yahoo] + earnings_sources
    validation["earnings_sources"] = earnings_sources

    yahoo_price = safe_float(yahoo.get("price"))
    if futu_price is not None and yahoo_price is not None:
        diff = yahoo_price - futu_price
        pct = abs(diff) / futu_price * 100 if futu_price else None
        status = "OK"
        if pct is not None and pct > 1.0:
            status = "WARN"
        validation["price_check"] = {
            "status": status,
            "futu_price": round(futu_price, 4),
            "yahoo_price": round(yahoo_price, 4),
            "absolute_diff": round(diff, 4),
            "percent_diff": round(pct, 3) if pct is not None else None,
            "warning": "Futu and Yahoo price differ by more than 1%." if status == "WARN" else None,
        }
    elif yahoo.get("status") == "ERROR":
        validation["price_check"] = {"status": "UNAVAILABLE", "error": yahoo.get("error")}

    days_to_earnings = safe_int(earnings.get("days_to_earnings")) if earnings else None
    if earnings and earnings.get("status") == "OK" and days_to_earnings is not None:
        status = "OK"
        warning = None
        if 0 <= days_to_earnings <= 14:
            status = "WARN"
            warning = "Earnings are within 14 days; avoid opening short options unless the earnings risk is intentional."
        elif 15 <= days_to_earnings <= 35:
            status = "ATTENTION"
            warning = "Earnings are within 35 days; prefer expiries before earnings or size smaller."
        validation["earnings_check"] = {
            "status": status,
            "next_earnings_date": earnings.get("next_earnings_date"),
            "days_to_earnings": days_to_earnings,
            "time_of_day": earnings.get("time_of_day"),
            "source": earnings.get("source"),
            "source_name": earnings.get("name"),
            "confirmation_status": earnings.get("confirmation_status"),
            "warning": warning,
        }
    else:
        errors = [
            f"{item.get('source')}: {item.get('error') or item.get('status')}"
            for item in earnings_sources
            if item.get("status") != "OK"
        ]
        validation["earnings_check"] = {"status": "UNAVAILABLE", "error": " | ".join(errors) if errors else "earnings date not found"}

    return validation
