import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

from pmcc.positions import normalize_option_code, parse_option_code_metadata
from pmcc.utils import parse_expiry, safe_float, safe_int, safe_text

BROKERS = {"FUTU", "SCHWAB", "MANUAL_UNKNOWN"}
STRATEGIES = {"PMCC", "CSP", "COVERED_CALL", "OTHER"}
CONFIDENCE_VALUES = {"confirmed", "needs_review"}
SOURCES = {"futu_opend", "manual_cli", "manual_csv", "schwab_export"}
EVENT_TYPES = {
    "OPEN_SHORT_CALL",
    "CLOSE_SHORT_CALL",
    "ROLL_SHORT_CALL",
    "OPEN_LONG_CALL",
    "CLOSE_LONG_CALL",
    "OPEN_SHORT_PUT",
    "CLOSE_SHORT_PUT",
}
SIDES = {"BUY_TO_OPEN", "SELL_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_CLOSE"}
EXPECTED_SIDE_BY_EVENT_TYPE = {
    "OPEN_SHORT_CALL": "SELL_TO_OPEN",
    "CLOSE_SHORT_CALL": "BUY_TO_CLOSE",
    "OPEN_LONG_CALL": "BUY_TO_OPEN",
    "CLOSE_LONG_CALL": "SELL_TO_CLOSE",
    "OPEN_SHORT_PUT": "SELL_TO_OPEN",
    "CLOSE_SHORT_PUT": "BUY_TO_CLOSE",
}
NUMERIC_FIELDS = [
    "price",
    "commission",
    "fees",
    "gross_amount",
    "net_amount",
    "realized_pnl",
    "profit_capture_pct",
    "dte",
    "delta",
    "iv",
]


def parse_trade_event_text(raw: str) -> Dict[str, Any]:
    event: Dict[str, Any] = {}
    for item in raw.split(";"):
        text = item.strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"Cannot parse trade event field: {text}")
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Cannot parse trade event field: {text}")
        event[key] = value.strip()
    return normalize_trade_event(event)


def normalize_trade_event(record: Dict[str, Any]) -> Dict[str, Any]:
    event = dict(record)

    for field in ["trade_id", "event_date", "account", "source_reference", "notes", "obsidian_note"]:
        text = safe_text(event.get(field))
        if text is not None:
            event[field] = text

    for field in ["broker", "symbol", "strategy", "event_type", "side", "underlying", "option_type"]:
        text = safe_text(event.get(field))
        if text is not None:
            event[field] = text.upper()

    for field in ["source", "confidence", "pmcc_report_version"]:
        text = safe_text(event.get(field))
        if text is not None:
            event[field] = text.lower()

    option_code = safe_text(event.get("option_code"))
    if option_code is not None:
        event["option_code"] = option_code
        event.setdefault("broker_option_symbol", option_code)
        canonical_key = normalize_option_code(option_code).upper()
        meta = parse_option_code_metadata(canonical_key)
        if meta.get("expiry") is not None and meta.get("strike") is not None and meta.get("option_type") is not None:
            event.setdefault("canonical_option_key", canonical_key)
        if meta.get("underlying"):
            event.setdefault("underlying", meta["underlying"].upper())
            event.setdefault("symbol", meta["underlying"].upper())
        if meta.get("expiry"):
            event.setdefault("expiry", meta["expiry"])
        if meta.get("strike") is not None:
            event.setdefault("strike", meta["strike"])
        if meta.get("option_type"):
            event.setdefault("option_type", meta["option_type"])

    quantity = safe_int(event.get("quantity"))
    if quantity is not None:
        event["quantity"] = quantity

    for field in ["strike"] + NUMERIC_FIELDS:
        value = safe_float(event.get(field))
        if value is not None:
            event[field] = value

    return event


def validate_trade_event(record: Dict[str, Any]) -> List[str]:
    event = normalize_trade_event(record)
    issues: List[str] = []

    for field in ["event_date", "broker", "symbol", "strategy", "event_type", "side", "quantity", "price", "source", "confidence"]:
        if event.get(field) in {None, ""}:
            issues.append(f"missing {field}")

    if event.get("event_date") is not None and parse_expiry(event.get("event_date")) is None:
        issues.append("event_date must be YYYY-MM-DD")

    if event.get("broker") is not None and event.get("broker") not in BROKERS:
        issues.append("broker is invalid")
    if event.get("strategy") is not None and event.get("strategy") not in STRATEGIES:
        issues.append("strategy is invalid")
    if event.get("event_type") is not None and event.get("event_type") not in EVENT_TYPES:
        issues.append("event_type is invalid")
    if event.get("side") is not None and event.get("side") not in SIDES:
        issues.append("side is invalid")
    if event.get("source") is not None and event.get("source") not in SOURCES:
        issues.append("source is invalid")
    if event.get("confidence") is not None and event.get("confidence") not in CONFIDENCE_VALUES:
        issues.append("confidence is invalid")

    quantity = safe_int(event.get("quantity"))
    if quantity is not None and quantity <= 0:
        issues.append("quantity must be positive")

    expected_side = EXPECTED_SIDE_BY_EVENT_TYPE.get(str(event.get("event_type") or ""))
    if expected_side is not None and event.get("side") is not None and event.get("side") != expected_side:
        issues.append(f"side should be {expected_side} for {event.get('event_type')}")

    option_key = _event_option_match_key(event)
    if option_key is not None:
        meta = parse_option_code_metadata(option_key)
        if meta.get("expiry") is None or meta.get("strike") is None or meta.get("option_type") is None:
            issues.append("option_code cannot be parsed")
        if event.get("expiry") is not None and meta.get("expiry") is not None and event.get("expiry") != meta.get("expiry"):
            issues.append("expiry conflicts with option_code")
        if event.get("strike") is not None and meta.get("strike") is not None and abs(float(event["strike"]) - float(meta["strike"])) > 0.0001:
            issues.append("strike conflicts with option_code")
        if event.get("option_type") is not None and meta.get("option_type") is not None and event.get("option_type") != meta.get("option_type"):
            issues.append("option_type conflicts with option_code")

    if event.get("broker") == "SCHWAB" and not safe_text(event.get("source_reference")):
        issues.append("source_reference is required for SCHWAB trades")

    return issues


def validate_trade_event_against_journal(record: Dict[str, Any], journal_events: List[Dict[str, Any]]) -> List[str]:
    event = normalize_trade_event(record)
    issues: List[str] = []
    event_type = safe_text(event.get("event_type"))
    if event_type not in {"CLOSE_SHORT_CALL", "CLOSE_SHORT_PUT", "CLOSE_LONG_CALL"}:
        return issues

    option_key = _event_option_match_key(event)
    if option_key is None:
        return issues

    expected_open_types = {
        "CLOSE_SHORT_CALL": {"OPEN_SHORT_CALL", "ROLL_SHORT_CALL"},
        "CLOSE_SHORT_PUT": {"OPEN_SHORT_PUT"},
        "CLOSE_LONG_CALL": {"OPEN_LONG_CALL"},
    }.get(event_type, set())
    has_open = any(
        _event_option_match_key(item) == option_key
        and safe_text(item.get("broker")) == safe_text(event.get("broker"))
        and safe_text(item.get("event_type")) in expected_open_types
        for item in (normalize_trade_event(recorded) for recorded in journal_events)
    )
    if not has_open:
        issues.append("close without known open position")
    return issues


def append_trade_event(path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    event = normalize_trade_event(record)
    if safe_text(event.get("trade_id")) is None:
        event["trade_id"] = f"trade-{uuid.uuid4().hex}"
    issues = validate_trade_event(event)
    if issues:
        raise ValueError("; ".join(issues))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def build_pmcc_short_call_cycles(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [normalize_trade_event(event) for event in events]
    realized_matches = build_realized_pnl_matches(normalized)
    closes = [
        event
        for event in normalized
        if event.get("strategy") == "PMCC" and event.get("event_type") == "CLOSE_SHORT_CALL"
    ]
    opens = [
        event
        for event in normalized
        if event.get("strategy") == "PMCC" and event.get("event_type") == "OPEN_SHORT_CALL"
    ]

    cycles: List[Dict[str, Any]] = []
    used_open_indexes = set()
    for close_event in closes:
        close_key = _cycle_match_key(close_event)
        open_match = None
        open_index = None
        for index, open_event in enumerate(opens):
            if index in used_open_indexes:
                continue
            if _cycle_match_key(open_event) == close_key:
                open_match = open_event
                open_index = index
                break
        if open_match is None or open_index is None:
            continue

        used_open_indexes.add(open_index)
        cycles.append(
            {
                "cycle_type": "PMCC_SHORT_CALL_ROLL",
                "event_date": close_event.get("event_date"),
                "broker": close_event.get("broker"),
                "account": close_event.get("account"),
                "symbol": close_event.get("symbol"),
                "close_event": close_event,
                "open_event": open_match,
                "quantity_closed": close_event.get("quantity"),
                "quantity_opened": open_match.get("quantity"),
                "realized_pnl": _realized_pnl_for_close(close_event, realized_matches),
                "profit_capture_pct": close_event.get("profit_capture_pct"),
                "reason_tags": _join_cycle_reason_tags(close_event, open_match),
                "notes": _join_cycle_notes(close_event, open_match),
            }
        )
    return cycles


def build_realized_pnl_matches(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [normalize_trade_event(event) for event in events]
    open_events: List[Dict[str, Any]] = []
    matches: List[Dict[str, Any]] = []

    for event in normalized:
        event_type = safe_text(event.get("event_type"))
        if event_type in {"OPEN_SHORT_CALL", "OPEN_SHORT_PUT", "OPEN_LONG_CALL"}:
            open_events.append(
                {
                    "event": event,
                    "remaining_quantity": safe_int(event.get("quantity")) or 0,
                }
            )
            continue
        if event_type not in {"CLOSE_SHORT_CALL", "CLOSE_SHORT_PUT", "CLOSE_LONG_CALL"}:
            continue
        close_remaining = safe_int(event.get("quantity")) or 0
        if close_remaining <= 0:
            continue

        expected_open_type = {
            "CLOSE_SHORT_CALL": "OPEN_SHORT_CALL",
            "CLOSE_SHORT_PUT": "OPEN_SHORT_PUT",
            "CLOSE_LONG_CALL": "OPEN_LONG_CALL",
        }.get(event_type)
        for open_record in open_events:
            open_event = open_record["event"]
            open_remaining = safe_int(open_record.get("remaining_quantity")) or 0
            if open_remaining <= 0:
                continue
            if safe_text(open_event.get("event_type")) != expected_open_type:
                continue
            if _open_close_match_key(open_event) != _open_close_match_key(event):
                continue
            matched_quantity = min(open_remaining, close_remaining)
            realized_pnl = calculate_realized_pnl_from_open_close(open_event, event, matched_quantity=matched_quantity)
            if realized_pnl is None:
                continue
            open_record["remaining_quantity"] = open_remaining - matched_quantity
            close_remaining -= matched_quantity
            matches.append(
                {
                    "open_event": open_event,
                    "close_event": event,
                    "matched_quantity": matched_quantity,
                    "open_remaining_quantity": open_record["remaining_quantity"],
                    "close_remaining_quantity": close_remaining,
                    "realized_pnl": realized_pnl,
                }
            )
            if close_remaining <= 0:
                break
    return matches


def calculate_realized_pnl_from_open_close(
    open_event: Dict[str, Any],
    close_event: Dict[str, Any],
    matched_quantity: Any = None,
) -> Any:
    open_normalized = normalize_trade_event(open_event)
    close_normalized = normalize_trade_event(close_event)
    open_price = safe_float(open_normalized.get("price"))
    close_price = safe_float(close_normalized.get("price"))
    quantity = safe_int(matched_quantity)
    if quantity is None:
        quantity = safe_int(close_normalized.get("quantity"))
    if open_price is None or close_price is None or quantity is None:
        return None

    open_side = safe_text(open_normalized.get("side"))
    close_side = safe_text(close_normalized.get("side"))
    if open_side in {"SELL_TO_OPEN"} and close_side in {"BUY_TO_CLOSE"}:
        gross = (open_price - close_price) * quantity * 100
    elif open_side in {"BUY_TO_OPEN"} and close_side in {"SELL_TO_CLOSE"}:
        gross = (close_price - open_price) * quantity * 100
    else:
        return None

    costs = 0.0
    for event in [open_normalized, close_normalized]:
        event_quantity = safe_int(event.get("quantity")) or quantity
        ratio = quantity / event_quantity if event_quantity > 0 else 1
        costs += ((safe_float(event.get("commission")) or 0.0) + (safe_float(event.get("fees")) or 0.0)) * ratio
    return round(gross - costs, 2)


def suggest_futu_open_event_drafts(
    current_positions: List[Any],
    journal_events: List[Dict[str, Any]],
    event_date: str,
    account: str = "",
) -> List[Dict[str, Any]]:
    recorded_open_codes = {
        _event_option_match_key(event)
        for event in (normalize_trade_event(item) for item in journal_events)
        if event.get("broker") == "FUTU" and str(event.get("event_type") or "").startswith("OPEN_")
    }
    drafts: List[Dict[str, Any]] = []
    for position in current_positions:
        code = safe_text(_position_field(position, "raw_code") or _position_field(position, "code") or _position_field(position, "option_code"))
        if code is None:
            continue
        code = normalize_option_code(code).upper()
        if normalize_option_code(code).upper() in recorded_open_codes:
            continue

        option_type = safe_text(_position_field(position, "option_type"))
        if option_type is None:
            option_type = safe_text(parse_option_code_metadata(code).get("option_type"))
        option_type = option_type.upper() if option_type is not None else None
        if option_type == "CALL":
            event_type = "OPEN_SHORT_CALL"
        elif option_type == "PUT":
            event_type = "OPEN_SHORT_PUT"
        else:
            event_type = "OPEN_SHORT_CALL"

        event = normalize_trade_event(
            {
                "event_date": event_date,
                "broker": "FUTU",
                "account": account,
                "strategy": "PMCC" if option_type == "CALL" else "CSP",
                "event_type": event_type,
                "option_code": code,
                "side": EXPECTED_SIDE_BY_EVENT_TYPE.get(event_type),
                "quantity": _position_field(position, "quantity"),
                "price": _position_field(position, "cost_price"),
                "source": "futu_opend",
                "source_reference": f"opend_position:{code}",
                "confidence": "needs_review",
                "notes": "Drafted from current Futu OpenD position; confirm before saving.",
            }
        )
        drafts.append({"event": event, "issues": validate_trade_event(event)})
    return drafts


def parse_schwab_trade_csv_text(csv_text: str, strategy: str = "PMCC") -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    drafts: List[Dict[str, Any]] = []
    for row_number, row in enumerate(reader, start=2):
        option_code = _first_row_value(row, ["option_code", "Option Code", "Symbol", "Instrument", "Description"])
        side = _normalize_side_from_action(_first_row_value(row, ["side", "Side", "Action", "Instruction"]))
        meta = parse_option_code_metadata(normalize_option_code(option_code or "").upper()) if option_code else {}
        event_type = _event_type_from_side_and_option(side, safe_text(meta.get("option_type")))
        event = normalize_trade_event(
            {
                "event_date": _first_row_value(row, ["event_date", "Date", "Trade Date", "Exec Time"]),
                "broker": "SCHWAB",
                "account": _first_row_value(row, ["account", "Account"]),
                "symbol": _first_row_value(row, ["Underlying", "underlying", "Ticker", "ticker"]) or meta.get("underlying"),
                "strategy": strategy,
                "event_type": event_type,
                "option_code": option_code,
                "side": side,
                "quantity": _normalize_schwab_number(_first_row_value(row, ["quantity", "Qty", "Quantity"]), absolute=True),
                "price": _normalize_schwab_number(_first_row_value(row, ["price", "Price", "Avg Price"]), absolute=True),
                "commission": _normalize_schwab_number(_first_row_value(row, ["commission", "Commission"]), absolute=True),
                "fees": _normalize_schwab_number(_first_row_value(row, ["fees", "Fees"]), absolute=True),
                "source": "schwab_export",
                "source_reference": _first_row_value(row, ["source_reference", "Order ID", "Activity ID"]) or f"csv-row-{row_number}",
                "confidence": "needs_review",
            }
        )
        drafts.append({"event": event, "issues": validate_trade_event(event)})
    return drafts


def build_obsidian_trade_note(events: List[Dict[str, Any]], reflection: str = "") -> str:
    normalized = [normalize_trade_event(event) for event in events]
    title_date = normalized[0].get("event_date") if normalized else "undated"
    title_symbol = normalized[0].get("symbol") if normalized else "trade"
    lines = [
        f"# Trade Review - {title_date} - {title_symbol}",
        "",
        "## Trade Events",
        "",
    ]
    for event in normalized:
        parts = [
            safe_text(event.get("event_date")) or "-",
            safe_text(event.get("broker")) or "-",
            safe_text(event.get("event_type")) or "-",
            safe_text(event.get("option_code")) or "-",
            f"qty {event.get('quantity') if event.get('quantity') is not None else '-'}",
            f"price {event.get('price') if event.get('price') is not None else '-'}",
            safe_text(event.get("confidence")) or "-",
        ]
        lines.append(f"- {' | '.join(parts)}")
        trade_id = safe_text(event.get("trade_id"))
        if trade_id is not None:
            lines.append(f"  trade_id: `{trade_id}`")
    lines.extend(["", "## Reflection", ""])
    lines.append(safe_text(reflection) or "Needs review.")
    lines.extend(["", "## Machine-Readable References", ""])
    for event in normalized:
        reference = safe_text(event.get("trade_id")) or safe_text(event.get("source_reference")) or safe_text(event.get("option_code"))
        if reference is not None:
            lines.append(f"- `{reference}`")
    return "\n".join(lines).rstrip() + "\n"


def _cycle_match_key(event: Dict[str, Any]) -> tuple:
    return (
        event.get("event_date"),
        event.get("broker"),
        event.get("account"),
        event.get("symbol"),
    )


def _open_close_match_key(event: Dict[str, Any]) -> tuple:
    return (
        event.get("broker"),
        event.get("account"),
        event.get("symbol"),
        _event_option_match_key(event),
    )


def _realized_pnl_for_close(close_event: Dict[str, Any], matches: List[Dict[str, Any]]) -> Any:
    for match in matches:
        if match.get("close_event") is close_event or _trade_event_identity(match.get("close_event") or {}) == _trade_event_identity(close_event):
            return match.get("realized_pnl")
    return None


def _trade_event_identity(event: Dict[str, Any]) -> tuple:
    return (
        event.get("event_date"),
        event.get("broker"),
        event.get("account"),
        event.get("symbol"),
        event.get("event_type"),
        event.get("side"),
        event.get("quantity"),
        event.get("price"),
        event.get("source_reference"),
        _event_option_match_key(event),
    )


def _event_option_match_key(event: Dict[str, Any]) -> Any:
    canonical_key = safe_text(event.get("canonical_option_key"))
    if canonical_key is not None:
        return normalize_option_code(canonical_key).upper()
    option_code = safe_text(event.get("option_code"))
    if option_code is None:
        return None
    return normalize_option_code(option_code).upper()


def _join_cycle_notes(close_event: Dict[str, Any], open_event: Dict[str, Any]) -> str:
    notes = [safe_text(close_event.get("notes")), safe_text(open_event.get("notes"))]
    return " | ".join(note for note in notes if note)


def _join_cycle_reason_tags(close_event: Dict[str, Any], open_event: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    for event in [close_event, open_event]:
        raw_tags = event.get("reason_tags")
        if isinstance(raw_tags, list):
            candidates = raw_tags
        else:
            candidates = [raw_tags, event.get("reason")]
        for candidate in candidates:
            tag = safe_text(candidate)
            if tag is not None and tag not in tags:
                tags.append(tag)
    return tags


def _position_field(position: Any, field: str) -> Any:
    if isinstance(position, dict):
        return position.get(field)
    return getattr(position, field, None)


def _first_row_value(row: Dict[str, Any], names: List[str]) -> Any:
    lowered = {key.lower(): value for key, value in row.items() if key is not None}
    for name in names:
        if name in row and safe_text(row.get(name)) is not None:
            return row.get(name)
        value = lowered.get(name.lower())
        if safe_text(value) is not None:
            return value
    return None


def _normalize_schwab_number(value: Any, absolute: bool = False) -> Any:
    text = safe_text(value)
    if text is None:
        return None
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    cleaned = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    numeric = safe_float(cleaned)
    if numeric is None:
        return value
    if negative and numeric > 0:
        numeric = -numeric
    if absolute:
        numeric = abs(numeric)
    return numeric


def _normalize_side_from_action(action: Any) -> Any:
    text = safe_text(action)
    if text is None:
        return None
    normalized = text.upper().replace(" ", "_")
    aliases = {
        "STO": "SELL_TO_OPEN",
        "SELL_TO_OPEN": "SELL_TO_OPEN",
        "SOLD_TO_OPEN": "SELL_TO_OPEN",
        "BTO": "BUY_TO_OPEN",
        "BUY_TO_OPEN": "BUY_TO_OPEN",
        "BOUGHT_TO_OPEN": "BUY_TO_OPEN",
        "BTC": "BUY_TO_CLOSE",
        "BUY_TO_CLOSE": "BUY_TO_CLOSE",
        "BOUGHT_TO_CLOSE": "BUY_TO_CLOSE",
        "STC": "SELL_TO_CLOSE",
        "SELL_TO_CLOSE": "SELL_TO_CLOSE",
        "SOLD_TO_CLOSE": "SELL_TO_CLOSE",
    }
    return aliases.get(normalized, normalized)


def _event_type_from_side_and_option(side: Any, option_type: Any) -> Any:
    if side == "SELL_TO_OPEN" and option_type == "CALL":
        return "OPEN_SHORT_CALL"
    if side == "BUY_TO_CLOSE" and option_type == "CALL":
        return "CLOSE_SHORT_CALL"
    if side == "SELL_TO_OPEN" and option_type == "PUT":
        return "OPEN_SHORT_PUT"
    if side == "BUY_TO_CLOSE" and option_type == "PUT":
        return "CLOSE_SHORT_PUT"
    if side == "BUY_TO_OPEN" and option_type == "CALL":
        return "OPEN_LONG_CALL"
    if side == "SELL_TO_CLOSE" and option_type == "CALL":
        return "CLOSE_LONG_CALL"
    return None


def read_trade_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                events.append(json.loads(text))
    return events
