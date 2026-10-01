"""CSV position source: run PMCC analysis without Futu OpenD.

Each run scans a directory for the latest exported position CSVs:

- Schwab: ``*PositionStatement.csv`` / ``*PositionStatement.txt``
  (thinkorswim Position Statement export, date embedded in the filename)
- Futu: ``持仓-*.csv`` (Futu 保证金综合账户 position export,
  datetime embedded in the filename, e.g. ``持仓-保证金综合账户(9123)-20260929-221417.csv``)

When a CSV newer than the last run is found it becomes the position source
and the saved JSON snapshots are refreshed. When no new CSV is available,
the previously recorded positions are reused untouched.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pmcc.models import PositionInput
from pmcc.positions import combine_positions_by_code, normalize_option_code, parse_option_code_metadata
from pmcc.utils import safe_float, safe_int, safe_text

TOS_MONTHS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def tos_option_code(underlying: str, instrument: str) -> Optional[str]:
    match = re.match(
        r"^100\s+(?:\(WEEKLYS\)\s+)?(?P<day>\d{1,2})\s+(?P<month>[A-Z]{3})\s+(?P<year>\d{2})\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<cp>CALL|PUT)$",
        instrument.strip().upper(),
    )
    if not match:
        return None
    month = TOS_MONTHS.get(match.group("month"))
    if month is None:
        return None
    day = int(match.group("day"))
    strike = float(match.group("strike"))
    strike_code = f"{int(round(strike * 1000)):06d}"
    cp = "C" if match.group("cp") == "CALL" else "P"
    return f"US.{underlying.upper()}{match.group('year')}{month}{day:02d}{cp}{strike_code}"


def parse_schwab_position_statement(path: Path) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    path = Path(str(path).strip())
    base_positions: List[PositionInput] = []
    short_positions: List[PositionInput] = []
    skipped: List[Dict[str, Any]] = []
    current_underlying: Optional[str] = None
    rows_seen = 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            rows_seen += 1
            if not row:
                continue
            instrument = safe_text(row[0])
            if not instrument or instrument in {"Instrument", "Equities and Equity Options"}:
                continue
            qty_text = safe_text(row[1]) if len(row) > 1 else None
            trade_price = safe_float(row[3]) if len(row) > 3 else None

            if qty_text is None and re.fullmatch(r"[A-Z]{1,6}", instrument):
                current_underlying = instrument
                continue

            if not instrument.startswith("100 "):
                continue
            if current_underlying is None:
                skipped.append({"instrument": instrument, "reason": "missing_underlying"})
                continue
            quantity_signed = safe_int(qty_text)
            if quantity_signed is None or quantity_signed == 0:
                skipped.append({"instrument": instrument, "qty": qty_text, "reason": "zero_or_missing_quantity"})
                continue
            code = tos_option_code(current_underlying, instrument)
            if code is None:
                skipped.append({"instrument": instrument, "reason": "unsupported_option_format"})
                continue
            meta = parse_option_code_metadata(code)
            position = PositionInput(
                raw_code=code,
                underlying=meta["underlying"],
                quantity=abs(quantity_signed),
                strike=meta["strike"],
                expiry=meta["expiry"],
                option_type=meta["option_type"],
                cost_price=trade_price,
            )
            if quantity_signed > 0 and position.option_type == "CALL":
                base_positions.append(position)
            elif quantity_signed < 0:
                short_positions.append(position)
            else:
                skipped.append({"instrument": instrument, "qty": qty_text, "reason": "long_put_not_pmcc_base"})

    return combine_positions_by_code(base_positions), combine_positions_by_code(short_positions), {
        "source": "thinkorswim_position_statement",
        "path": str(path),
        "rows_seen": rows_seen,
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_contracts": sum(item.quantity for item in short_positions),
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Futu position CSV
# ---------------------------------------------------------------------------

FUTU_POSITION_CSV_GLOBS = ["持仓-*.csv"]
FUTU_OPTION_CODE_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d+)$")


def parse_futu_position_csv(path: Path) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    """Parse a Futu 保证金综合账户 position export CSV.

    Only option legs become PMCC positions: positive-quantity CALLs are treated
    as base (LEAPS) legs, negative quantities as short legs (calls and puts).
    Stock rows, strategy-summary rows (e.g. ``MSFT270617C390/480``) and
    zero-quantity rows are skipped and reported in metadata.
    """
    path = Path(str(path).strip())
    base_positions: List[PositionInput] = []
    short_positions: List[PositionInput] = []
    skipped: List[Dict[str, Any]] = []
    rows_seen = 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            rows_seen += 1
            if not row:
                continue
            code_raw = safe_text(row[0])
            if not code_raw or code_raw == "代码":
                continue
            if "/" in code_raw:
                skipped.append({"code": code_raw, "reason": "strategy_summary_row"})
                continue
            if not FUTU_OPTION_CODE_RE.match(code_raw.strip().upper()):
                continue
            qty_text = safe_text(row[2]) if len(row) > 2 else None
            cost_price = safe_float(row[4]) if len(row) > 4 else None
            quantity_signed = safe_int(qty_text)
            if quantity_signed is None or quantity_signed == 0:
                skipped.append({"code": code_raw, "qty": qty_text, "reason": "zero_or_missing_quantity"})
                continue
            code = "US." + normalize_option_code(code_raw)
            meta = parse_option_code_metadata(code)
            position = PositionInput(
                raw_code=code,
                underlying=meta["underlying"],
                quantity=abs(quantity_signed),
                strike=meta["strike"],
                expiry=meta["expiry"],
                option_type=meta["option_type"],
                cost_price=cost_price,
            )
            if quantity_signed > 0 and position.option_type == "CALL":
                base_positions.append(position)
            elif quantity_signed < 0:
                short_positions.append(position)
            else:
                skipped.append({"code": code_raw, "qty": qty_text, "reason": "long_put_not_pmcc_base"})

    return combine_positions_by_code(base_positions), combine_positions_by_code(short_positions), {
        "source": "futu_position_csv",
        "path": str(path),
        "rows_seen": rows_seen,
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_contracts": sum(item.quantity for item in short_positions),
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Latest-CSV discovery
# ---------------------------------------------------------------------------

SCHWAB_POSITION_CSV_GLOBS = ["*PositionStatement.csv", "*PositionStatement.txt"]
SCHWAB_FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
FUTU_FILENAME_DATETIME_RE = re.compile(r"(\d{8})-(\d{6})")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _schwab_sort_key(path: Path) -> Tuple[str, float]:
    match = SCHWAB_FILENAME_DATE_RE.search(path.name)
    return (match.group(1) if match else "", _mtime(path))


def _futu_sort_key(path: Path) -> Tuple[str, float]:
    match = FUTU_FILENAME_DATETIME_RE.search(path.name)
    return ((match.group(1) + match.group(2)) if match else "", _mtime(path))


def find_latest_position_csvs(csv_dir: Path) -> Dict[str, Optional[Path]]:
    """Return the newest Schwab / Futu position CSVs under *csv_dir*.

    Ordering is by the date embedded in the filename first, then by file
    mtime as a tiebreaker. Returns ``{"schwab": Path | None, "futu": Path | None}``.
    """
    csv_dir = Path(csv_dir)
    result: Dict[str, Optional[Path]] = {"schwab": None, "futu": None}
    if not csv_dir.is_dir():
        return result
    schwab_candidates = [p for glob in SCHWAB_POSITION_CSV_GLOBS for p in csv_dir.glob(glob) if p.is_file()]
    futu_candidates = [p for glob in FUTU_POSITION_CSV_GLOBS for p in csv_dir.glob(glob) if p.is_file()]
    if schwab_candidates:
        result["schwab"] = max(schwab_candidates, key=_schwab_sort_key)
    if futu_candidates:
        result["futu"] = max(futu_candidates, key=_futu_sort_key)
    return result


# ---------------------------------------------------------------------------
# "Is there a new CSV?" state tracking
# ---------------------------------------------------------------------------


def load_csv_state(state_path: Path) -> Dict[str, Any]:
    """Load the last-used CSV record; returns {} when missing or corrupt."""
    try:
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_csv_state(state_path: Path, records: Dict[str, Dict[str, Any]]) -> None:
    """Persist which CSV files were consumed by the latest run."""
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "files": records,
    }
    Path(state_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def is_new_csv(path: Optional[Path], record: Optional[Dict[str, Any]]) -> bool:
    """True when *path* is a CSV we have not consumed yet.

    A file counts as new when its name differs from the recorded one, or when
    the same name was re-exported (mtime moved forward).
    """
    if path is None or not path.is_file():
        return False
    if not record:
        return True
    if path.name != record.get("name"):
        return True
    try:
        old_mtime = float(record.get("mtime") or 0)
    except (TypeError, ValueError):
        return True
    return _mtime(path) > old_mtime + 1.0
