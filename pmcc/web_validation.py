import html
import json
import re
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Dict, List, Optional

from pmcc.data_futu import fetch_opend_earnings_calendar
from pmcc.utils import parse_expiry, safe_float, safe_int, safe_text, symbol_to_web_ticker


TIER_PRIMARY = "primary"
TIER_CHECK = "check"
TIER_EVENT = "event"
TIER_MANUAL = "manual"

# Sources whose earnings date is human-verified / machine-authoritative.
MANUAL_CONFIRMED_SOURCE = "manual_confirmed"
WEB_AGREED_SOURCE = "web_agreed"


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
        "tier": TIER_EVENT,
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
        "tier": TIER_CHECK,
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
        "tier": TIER_EVENT,
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
        "ADBE": "adobe",
        "AMD": "amd",
        "AMZN": "amazon",
        "ARM": "arm-holdings",
        "AVGO": "broadcom",
        "BA": "boeing",
        "BAC": "bank-of-america",
        "BKNG": "booking-holdings",
        "CAT": "caterpillar",
        "CRM": "salesforce",
        "CSCO": "cisco",
        "CVX": "chevron",
        "DIS": "walt-disney",
        "GOOG": "alphabet",
        "GOOGL": "alphabet",
        "GS": "goldman-sachs",
        "HD": "home-depot",
        "IBM": "ibm",
        "INTC": "intel",
        "JNJ": "johnson-and-johnson",
        "JPM": "jpmorgan-chase",
        "KO": "coca-cola",
        "LLY": "eli-lilly",
        "MA": "mastercard",
        "MCD": "mcdonalds",
        "META": "meta",
        "MRK": "merck",
        "MSFT": "microsoft",
        "NFLX": "netflix",
        "NKE": "nike",
        "NVDA": "nvidia",
        "ORCL": "oracle",
        "PANW": "palo-alto-networks",
        "PEP": "pepsico",
        "PFE": "pfizer",
        "PG": "procter-and-gamble",
        "PLTR": "palantir-technologies",
        "QCOM": "qualcomm",
        "SBUX": "starbucks",
        "SOFI": "sofi-technologies",
        "SPGI": "s-and-p-global",
        "TSLA": "tesla",
        "TXN": "texas-instruments",
        "UNH": "united-health-group",
        "V": "visa",
        "VZ": "verizon",
        "WMT": "walmart",
        "XOM": "exxon-mobil",
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
    """Legacy pick-earliest helper, kept for the web-only fallback display path.

    The primary/confirmed decision is made by :func:`resolve_earnings_primary`; this
    function intentionally does NOT decide whether the date may hard-block trading.
    """
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


def earnings_record_date(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the ISO date of a usable earnings record, else None."""
    if not record or record.get("status") != "OK":
        return None
    days = safe_int(record.get("days_to_earnings"))
    if days is None or days < 0:
        return None
    text = safe_text(record.get("next_earnings_date"))
    if text is None:
        return None
    return text[:10]


def manual_earnings_record(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one local event-calendar entry into an earnings source record."""
    if not isinstance(event, dict):
        return None
    record_date = safe_text(event.get("date")) or safe_text(event.get("event_date"))
    days = safe_int(event.get("days_to_event"))
    if days is None:
        parsed = parse_expiry(record_date)
        if parsed is None:
            return None
        days = (parsed - date.today()).days
        record_date = parsed.isoformat()
    if record_date is None:
        return None
    source = safe_text(event.get("source")) or "local_event_calendar"
    manual_confirmed = source.lower() == MANUAL_CONFIRMED_SOURCE and days >= 0
    return {
        "name": "Local Event Calendar (manual confirmed)" if manual_confirmed else "Local Event Calendar",
        "source": source,
        "status": "OK",
        "tier": TIER_MANUAL,
        "next_earnings_date": record_date[:10],
        "days_to_earnings": days,
        "time_of_day": safe_text(event.get("time")) or safe_text(event.get("time_of_day")),
        "confirmation_status": "manual_confirmed" if manual_confirmed else "manual_unconfirmed",
        "manual_confirmed": manual_confirmed,
    }


def build_manual_earnings_records(events: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "event").lower() != "earnings":
            continue
        record = manual_earnings_record(event)
        if record is not None:
            records.append(record)
    return records


def candidate_date_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": record.get("source"),
        "name": record.get("name"),
        "tier": record.get("tier"),
        "date": earnings_record_date(record),
        "days_to_earnings": safe_int(record.get("days_to_earnings")),
        "time_of_day": record.get("time_of_day"),
        "confirmation_status": record.get("confirmation_status"),
    }


def resolve_earnings_primary(
    opend_record: Optional[Dict[str, Any]],
    web_records: List[Dict[str, Any]],
    manual_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Choose the primary earnings date and decide whether it is confirmed.

    Precedence:
      1. a usable Futu OpenD earnings-calendar record is primary (confirmed),
      2. else two agreeing web sources are primary, marked ``web_agreed`` (confirmed),
      3. else a date is still shown for display but ``confirmed`` stays False
         (a human-verified ``manual_confirmed`` entry is the one exception - it counts).
    ``source_conflict`` is True when the OpenD date differs from any OK web date, or when
    the two web sources disagree; every candidate date is returned for the report.
    """
    manual_records = manual_records or []
    opend_date = earnings_record_date(opend_record)
    web_ok = [record for record in web_records if earnings_record_date(record) is not None]
    web_dates = [earnings_record_date(record) for record in web_ok]
    unique_web_dates = sorted({value for value in web_dates if value})
    manual_ok = [record for record in manual_records if earnings_record_date(record) is not None]

    source_conflict = False
    if opend_date is not None and any(value != opend_date for value in web_dates):
        source_conflict = True
    if len(unique_web_dates) > 1:
        source_conflict = True

    primary: Optional[Dict[str, Any]] = None
    confirmed = False
    if opend_date is not None and opend_record is not None:
        primary = dict(opend_record)
        primary["tier"] = primary.get("tier") or TIER_PRIMARY
        primary["primary_reason"] = "opend"
        confirmed = True
    elif len(web_ok) >= 2 and len(unique_web_dates) == 1:
        base = choose_primary_earnings_source(web_ok) or web_ok[0]
        primary = dict(base)
        primary["source"] = WEB_AGREED_SOURCE
        primary["name"] = "Web sources agreed"
        primary["confirmation_status"] = WEB_AGREED_SOURCE
        primary["agreed_sources"] = [
            {"source": record.get("source"), "name": record.get("name"), "date": earnings_record_date(record)}
            for record in web_ok
        ]
        primary["primary_reason"] = WEB_AGREED_SOURCE
        confirmed = True
    else:
        web_primary = choose_primary_earnings_source(web_ok)
        manual_primary = None
        if manual_ok:
            manual_sorted = sorted(
                manual_ok,
                key=lambda item: (
                    0 if item.get("manual_confirmed") else 1,
                    safe_int(item.get("days_to_earnings")) or 999999,
                ),
            )
            manual_primary = manual_sorted[0]
        if web_primary is not None:
            primary = dict(web_primary)
            primary_date = earnings_record_date(primary)
            manual_support = [
                record
                for record in manual_ok
                if record.get("manual_confirmed") and earnings_record_date(record) == primary_date
            ]
            if manual_support:
                # A single web source is unconfirmed unless a human-verified local
                # entry backs the exact same date.
                primary["primary_reason"] = "web_supported_by_manual_confirmed"
                primary["manual_confirmed_support"] = True
                confirmed = True
            else:
                primary["primary_reason"] = "web_unconfirmed"
                confirmed = False
        elif manual_primary is not None:
            primary = dict(manual_primary)
            primary["primary_reason"] = "manual_confirmed" if manual_primary.get("manual_confirmed") else "manual_unconfirmed"
            confirmed = bool(manual_primary.get("manual_confirmed"))

    candidates = [candidate_date_entry(record) for record in ([opend_record] if opend_record else []) + web_ok + manual_ok]
    conflict_dates = [
        entry
        for entry in candidates
        if entry.get("date")
        and str(entry.get("tier")) in {TIER_PRIMARY, TIER_EVENT}
    ] if source_conflict else []

    return {
        "primary": primary,
        "confirmed": confirmed,
        "source_conflict": source_conflict,
        "candidate_dates": [entry for entry in candidates if entry.get("date")],
        "conflict_dates": conflict_dates,
    }


def build_market_data_validation(
    symbol: str,
    futu_price: Optional[float],
    enabled: bool = True,
    quote_ctx: Optional[Any] = None,
    manual_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validate Futu market data against independent sources.

    *quote_ctx* is optional: opend mode passes the live ``OpenQuoteContext`` so the
    quote-only Futu earnings calendar can act as the primary earnings source, while
    CBOE/CSV modes and existing callers keep working unchanged without it.
    *manual_events* are local event-calendar entries (human input) used for display and
    for the confirmed/unconfirmed decision only.
    """
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
    yahoo.setdefault("tier", TIER_CHECK)
    web_earnings_sources = [
        fetch_next_earnings_date(symbol),
        fetch_wallstreethorizon_earnings_date(symbol),
    ]
    for record in web_earnings_sources:
        record.setdefault("tier", TIER_EVENT)
    opend_earnings: Optional[Dict[str, Any]] = None
    if quote_ctx is not None:
        opend_earnings = fetch_opend_earnings_calendar(symbol, quote_ctx)
        opend_earnings.setdefault("tier", TIER_PRIMARY)
    manual_earnings = build_manual_earnings_records(manual_events)
    resolution = resolve_earnings_primary(opend_earnings, web_earnings_sources, manual_earnings)
    earnings = resolution["primary"]

    sources: List[Dict[str, Any]] = [yahoo]
    if opend_earnings is not None:
        sources.append(opend_earnings)
    sources.extend(web_earnings_sources)
    sources.extend(manual_earnings)
    validation["sources"] = sources
    validation["earnings_sources"] = ([opend_earnings] if opend_earnings is not None else []) + web_earnings_sources + manual_earnings
    validation["opend_earnings"] = opend_earnings

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
            "primary_reason": earnings.get("primary_reason"),
            "confirmed": bool(resolution["confirmed"]),
            "source_conflict": bool(resolution["source_conflict"]),
            "candidate_dates": resolution["candidate_dates"],
            "conflict_dates": resolution["conflict_dates"],
            "warning": warning,
        }
    else:
        errors = [
            f"{item.get('source')}: {item.get('error') or item.get('status')}"
            for item in validation["earnings_sources"]
            if item.get("status") != "OK"
        ]
        validation["earnings_check"] = {
            "status": "UNAVAILABLE",
            "error": " | ".join(errors) if errors else "earnings date not found",
            "confirmed": False,
            "source_conflict": bool(resolution["source_conflict"]),
            "candidate_dates": resolution["candidate_dates"],
            "conflict_dates": resolution["conflict_dates"],
        }

    return validation
