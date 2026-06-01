import re
from typing import Any, Dict, List, Optional

from pmcc.models import PositionInput
from pmcc.utils import safe_float, safe_int, safe_text


def normalize_option_code(code: str) -> str:
    return re.sub(r"\s+", "", code.strip())


def parse_option_code_metadata(code: str) -> Dict[str, Any]:
    normalized_code = normalize_option_code(code)
    match = re.match(r"^(?P<underlying>.+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d+)$", normalized_code)
    if not match:
        return {"underlying": normalized_code, "strike": None, "expiry": None, "option_type": None}

    ymd = match.group("ymd")
    expiry = f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}"
    strike = int(match.group("strike")) / 1000
    return {
        "underlying": match.group("underlying"),
        "strike": strike,
        "expiry": expiry,
        "option_type": "CALL" if match.group("cp") == "C" else "PUT",
    }


def parse_positions_input(raw: str) -> List[PositionInput]:
    normalized = raw.replace("；", ";").replace("，", ",")
    entries = [item.strip() for item in normalized.split(";") if item.strip()]
    positions: List[PositionInput] = []

    for entry in entries:
        parts = [part.strip() for part in entry.split(",") if part.strip()]
        if len(parts) not in {2, 3}:
            raise ValueError(f"Cannot parse entry: {entry}. Use CODE,QTY or CODE,QTY,COST and ';' between positions.")

        quantity = int(parts[1])
        cost_price = float(parts[2]) if len(parts) == 3 else None
        code = normalize_option_code(parts[0])
        meta = parse_option_code_metadata(code)
        positions.append(
            PositionInput(
                raw_code=code,
                underlying=meta["underlying"],
                quantity=quantity,
                strike=meta["strike"],
                expiry=meta["expiry"],
                option_type=meta["option_type"],
                cost_price=cost_price,
            )
        )

    return positions


def positions_to_compact_text(positions: List[PositionInput]) -> str:
    return ";".join(
        f"{item.raw_code},{item.quantity},{item.cost_price}" if item.cost_price is not None else f"{item.raw_code},{item.quantity}"
        for item in positions
    )


def position_to_record(position: PositionInput) -> Dict[str, Any]:
    return {
        "code": position.raw_code,
        "underlying": position.underlying,
        "quantity": position.quantity,
        "strike": position.strike,
        "expiry": position.expiry,
        "option_type": position.option_type,
        "cost_price": position.cost_price,
    }


def position_from_record(record: Dict[str, Any]) -> Optional[PositionInput]:
    code = safe_text(record.get("code") or record.get("raw_code"))
    quantity = safe_int(record.get("quantity"))
    if code is None or quantity is None or quantity <= 0:
        return None

    code = normalize_option_code(code)
    meta = parse_option_code_metadata(code)
    return PositionInput(
        raw_code=code,
        underlying=safe_text(record.get("underlying")) or meta["underlying"],
        quantity=quantity,
        strike=safe_float(record.get("strike")) if record.get("strike") is not None else meta["strike"],
        expiry=safe_text(record.get("expiry")) or meta["expiry"],
        option_type=safe_text(record.get("option_type")) or meta["option_type"],
        cost_price=safe_float(record.get("cost_price")),
    )


def positions_from_records(records: Any) -> List[PositionInput]:
    if not isinstance(records, list):
        return []

    positions: List[PositionInput] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        position = position_from_record(record)
        if position is not None:
            positions.append(position)
    return positions


def group_positions_by_underlying(positions: List[PositionInput]) -> Dict[str, List[PositionInput]]:
    grouped: Dict[str, List[PositionInput]] = {}
    for item in positions:
        grouped.setdefault(item.underlying, []).append(item)
    return grouped
