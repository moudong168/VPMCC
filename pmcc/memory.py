import json
from pathlib import Path
from typing import Any, Dict, List

from pmcc.models import PositionInput
from pmcc.positions import position_to_record, positions_from_records, positions_to_compact_text


def read_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_object(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_position_memory_payload(path: Path) -> Dict[str, Any]:
    return read_json_object(path)


def load_position_memory(path: Path) -> Dict[str, str]:
    data = load_position_memory_payload(path)
    base_records = positions_from_records(data.get("base_position_records"))
    short_records = positions_from_records(data.get("short_call_records"))
    return {
        "base_positions": positions_to_compact_text(base_records) if base_records else str(data.get("base_positions", "") or ""),
        "short_calls": positions_to_compact_text(short_records) if short_records else str(data.get("short_calls", "") or ""),
    }


def save_position_memory(
    path: Path,
    base_positions: List[PositionInput],
    short_calls: List[PositionInput],
    memory_scope: str = "analysis_snapshot",
) -> None:
    payload = {
        "memory_scope": memory_scope,
        "base_positions": positions_to_compact_text(base_positions),
        "short_calls": positions_to_compact_text(short_calls),
        "base_position_records": [position_to_record(item) for item in base_positions],
        "short_call_records": [position_to_record(item) for item in short_calls],
    }
    write_json_object(path, payload)
