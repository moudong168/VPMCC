import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from pmcc.interaction import (
    collect_external_positions_interactive as collect_external_positions_interactive_from_interaction,
    collect_iv_rank_overrides_for_symbols as collect_iv_rank_overrides_for_symbols_from_interaction,
    collect_positions_interactive as collect_positions_interactive_from_interaction,
    prompt_confirm_or_override_float as prompt_confirm_or_override_float_from_interaction,
    prompt_confirm_or_override_trend as prompt_confirm_or_override_trend_from_interaction,
    prompt_optional_float as prompt_optional_float_from_interaction,
    prompt_optional_positions as prompt_optional_positions_from_interaction,
    prompt_optional_trend as prompt_optional_trend_from_interaction,
    prompt_positions_with_memory as prompt_positions_with_memory_from_interaction,
    prompt_required_text as prompt_required_text_from_interaction,
    prompt_short_call_manual_metrics as prompt_short_call_manual_metrics_from_interaction,
    read_interactive_text,
    write_interactive_message,
)
from pmcc.errors import DataQualityError
from pmcc.constants import (
    IV_HISTORY_LOOKBACK_LIMIT,
    IV_HISTORY_MAX_RECORDS_PER_SYMBOL,
    IV_RANK_MEMORY_MAX_AGE_DAYS,
    VALID_TRENDS,
)
from pmcc.data_quality import build_option_data_quality, require_option_data_quality
from pmcc.data_futu import (
    collect_positions_from_opend as collect_positions_from_opend_from_futu,
    estimate_historical_volatility,
    format_futu_error,
    get_daily_klines,
    get_greeks,
    get_option_chain,
    get_option_expiries,
    get_preferred_expiry_dates,
    get_quote,
    get_trend,
    normalize_option_chain,
    throttle_option_chain_request,
)
from pmcc.models import (
    EventRiskBlock,
    PositionInput,
    ShortCallReview,
    ShortPutReview,
    StrategyConfig,
)
from pmcc.memory import (
    load_position_memory as load_position_memory_from_path,
    load_position_memory_payload as load_position_memory_payload_from_path,
    read_json_object,
    save_position_memory as save_position_memory_to_path,
    write_json_object,
)
from pmcc.reports import (
    clean_sentence_fragment,
    explain_action,
    explain_reason,
    explain_trend,
    event_risk_class,
    effective_candidate_capacity,
    build_chinese_summary,
    build_chinese_summary_clean,
    build_html_report,
    build_short_put_operation_advice,
    build_trader_summary_report,
    format_candidate_for_human,
    format_leaps_slot_rows,
    format_leaps_slot_summary,
    format_value,
    format_liquidity_summary,
    format_long_leg_expiry_rows,
    format_long_leg_expiry_summary,
    format_net_roll_price,
    format_plain_text_report,
    format_symbol_human,
    format_terminal_table,
    grouped_row_classes,
    html_leaps_slot_table,
    html_long_leg_expiry_table,
    html_table,
    html_text,
    html_value,
    render_html_report as render_html_report_from_reports,
    risk_class,
    risk_rank,
    short_call_risk_light,
    short_put_risk_light,
    summarize_data_validation,
    summarize_event_block,
    summarize_iv_environment,
    summarize_iv_rank_analysis,
    symbol_risk_light,
    validation_risk_class,
    write_html_report as write_html_report_to_path,
)
from pmcc.roll_pnl import estimate_whole_symbol_roll_pnl
from pmcc.iv import build_iv_environment
from pmcc.trade_journal import (
    append_trade_event,
    build_obsidian_trade_note,
    parse_schwab_trade_csv_text,
    parse_trade_event_text,
    read_trade_events,
    validate_trade_event,
)
from pmcc.web_validation import (
    build_earnings_source_record,
    build_market_data_validation,
    choose_primary_earnings_source,
    fetch_next_earnings_date,
    fetch_wallstreethorizon_earnings_date,
    fetch_yahoo_finance_quote,
    http_get_text,
    visible_page_text,
    wallstreethorizon_slug,
)
from pmcc.strategy import (
    assess_option_liquidity,
    build_action,
    evaluate_candidate_structure,
    score_dte,
    score_moneyness,
    score_options,
    select_option,
)
from pmcc.positions import (
    group_positions_by_underlying,
    parse_option_code_metadata,
    parse_positions_input,
    position_from_record,
    position_to_record,
    positions_from_records,
    positions_to_compact_text,
)
from pmcc.utils import parse_expiry, safe_float, safe_int, safe_text, symbol_to_web_ticker


def configure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_utf8_stdio()
FUTU_RUNTIME_APPDATA = Path(__file__).with_name(".runtime-appdata")
os.environ["APPDATA"] = str(FUTU_RUNTIME_APPDATA)
os.environ["appdata"] = str(FUTU_RUNTIME_APPDATA)


def patch_futu_log_makedirs() -> None:
    original_makedirs = os.makedirs

    def compatible_makedirs(name: Any, mode: int = 0o777, exist_ok: bool = False) -> None:
        try:
            original_makedirs(name, mode=mode, exist_ok=exist_ok)
        except FileExistsError:
            normalized = os.path.normpath(os.fspath(name))
            futu_log_suffix = os.path.normpath(os.path.join("com.futunn.FutuOpenD", "Log"))
            if normalized.endswith(futu_log_suffix):
                return
            raise

    os.makedirs = compatible_makedirs


patch_futu_log_makedirs()
from futu import OpenQuoteContext, OptionType, RET_OK, TrdEnv


HOST = "127.0.0.1"
PORT = 11111
INTERACTIVE_MODE = False
MEMORY_FILE = Path(__file__).with_name("pmcc_last_positions.json")
FUTU_POSITIONS_FILE = Path(__file__).with_name("pmcc_futu_positions.json")
SCHWAB_POSITIONS_FILE = Path(__file__).with_name("pmcc_schwab_positions.json")
IV_HISTORY_FILE = Path(__file__).with_name("pmcc_iv_history.json")
IV_RANK_MEMORY_FILE = Path(__file__).with_name("pmcc_iv_rank_memory.json")
EVENT_CALENDAR_FILE = Path(__file__).with_name("pmcc_event_calendar.json")
PRIVATE_DATA_DIR = Path(os.environ.get("PMCC_DATA_DIR", Path.home() / ".pmcc"))
TRADE_JOURNAL_FILE = PRIVATE_DATA_DIR / "pmcc_trade_journal.jsonl"
DEFAULT_CONFIG = StrategyConfig()


def load_local_event_records(symbol: str) -> List[Dict[str, Any]]:
    if not EVENT_CALENDAR_FILE.exists():
        return []
    try:
        payload = json.loads(EVENT_CALENDAR_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    symbol_key = symbol.upper()
    ticker_key = symbol_to_web_ticker(symbol)
    raw_records: Any = []
    if isinstance(payload, dict):
        raw_records = payload.get(symbol_key) or payload.get(ticker_key) or []
    if isinstance(raw_records, dict):
        raw_records = raw_records.get("events") or []
    if not isinstance(raw_records, list):
        return []

    records: List[Dict[str, Any]] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        event_date = parse_expiry(item.get("date") or item.get("event_date") or item.get("ex_dividend_date"))
        if event_date is None:
            continue
        records.append(
            {
                "type": safe_text(item.get("type")) or "event",
                "date": event_date.isoformat(),
                "days_to_event": (event_date - date.today()).days,
                "description": safe_text(item.get("description")) or safe_text(item.get("name")),
                "source": "local_event_calendar",
            }
        )
    return records


def extract_ex_dividend_event(quote: pd.Series) -> Optional[Dict[str, Any]]:
    for key in ["ex_dividend_date", "ex_div_date", "ex_dividend_day", "ex_date"]:
        if key not in quote:
            continue
        ex_date = parse_expiry(quote.get(key))
        if ex_date is None:
            continue
        return {
            "type": "ex_dividend",
            "date": ex_date.isoformat(),
            "days_to_event": (ex_date - date.today()).days,
            "description": "Ex-dividend date from quote snapshot",
            "source": "Futu OpenD quote",
        }
    return None


def build_event_risk_block(
    symbol: str,
    quote: pd.Series,
    validation: Dict[str, Any],
    config: StrategyConfig,
) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    earnings = validation.get("earnings_check") or {}
    earnings_days = safe_int(earnings.get("days_to_earnings"))
    if earnings_days is not None and earnings.get("next_earnings_date"):
        events.append(
            {
                "type": "earnings",
                "date": earnings.get("next_earnings_date"),
                "days_to_event": earnings_days,
                "description": f"Next earnings date ({earnings.get('time_of_day') or 'time unknown'})",
                "source": earnings.get("source") or "market_data_validation",
                "source_name": earnings.get("source_name"),
                "confirmation_status": earnings.get("confirmation_status"),
            }
        )
    ex_dividend = extract_ex_dividend_event(quote)
    if ex_dividend:
        events.append(ex_dividend)
    events.extend(load_local_event_records(symbol))

    blocking_events: List[Dict[str, Any]] = []
    attention_events: List[Dict[str, Any]] = []
    for event in events:
        days = safe_int(event.get("days_to_event"))
        if days is None or days < 0:
            continue
        event_type = str(event.get("type") or "event").lower()
        block_days = config.event_block_days
        if event_type == "earnings":
            block_days = config.earnings_block_days
        elif event_type in {"ex_dividend", "ex-dividend", "ex_div"}:
            block_days = config.ex_dividend_block_days
        if days <= block_days:
            blocking_events.append(event)
        elif days <= max(config.event_block_days, config.earnings_block_days, 35):
            attention_events.append(event)

    return EventRiskBlock(
        enabled=True,
        override_allowed=config.allow_event_short_call,
        blocked=bool(blocking_events) and not config.allow_event_short_call,
        blocking_events=blocking_events,
        attention_events=attention_events,
        event_calendar_file=str(EVENT_CALENDAR_FILE),
    ).to_dict()


def prompt_required_text(label: str) -> str:
    return prompt_required_text_from_interaction(label)


def prompt_optional_float(label: str) -> Optional[float]:
    return prompt_optional_float_from_interaction(label)


def prompt_confirm_or_override_float(label: str, current_value: Optional[float]) -> Optional[float]:
    return prompt_confirm_or_override_float_from_interaction(label, current_value)


def prompt_optional_trend(label: str) -> Optional[str]:
    return prompt_optional_trend_from_interaction(label)


def prompt_confirm_or_override_trend(label: str, current_value: str) -> str:
    return prompt_confirm_or_override_trend_from_interaction(label, current_value)


def prompt_positions(label: str) -> List[PositionInput]:
    while True:
        try:
            return parse_positions_input(prompt_required_text(label))
        except ValueError as exc:
            print(str(exc))


def format_position_inventory_line(position: PositionInput) -> str:
    strike = f"{position.strike:.2f}" if position.strike is not None else "-"
    expiry = position.expiry or "-"
    cost_price = f"{position.cost_price:.2f}" if position.cost_price is not None else "-"
    return (
        f"{position.underlying:<8}  "
        f"{position.raw_code:<22}  "
        f"{position.quantity:>3}  "
        f"{strike:>8}  "
        f"{expiry:<10}  "
        f"{cost_price:>10}"
    )


def print_position_inventory_before_analysis(base_positions: List[PositionInput], short_calls: List[PositionInput]) -> None:
    short_call_legs = [item for item in short_calls if item.option_type == "CALL"]
    short_put_legs = [item for item in short_calls if item.option_type == "PUT"]
    other_short_legs = [item for item in short_calls if item.option_type not in {"CALL", "PUT"}]
    print("")
    print("========== 分析前持仓确认 ==========")
    print(
        f"本次将用于 PMCC 分析的底仓：{sum(item.quantity for item in base_positions)} 张；"
        f"已卖 short call：{sum(item.quantity for item in short_call_legs)} 张；"
        f"已卖 short put：{sum(item.quantity for item in short_put_legs)} 张。"
    )

    print("")
    print("【LEAPS / 底仓】")
    if base_positions:
        print("标的      合约代码                  数量      行权价  到期日            成本")
        for position in sorted(base_positions, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    else:
        print("无")

    print("")
    print("【已卖 Short Call】")
    if short_call_legs:
        print("标的      合约代码                  数量      行权价  到期日            成本")
        for position in sorted(short_call_legs, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    else:
        print("无")

    print("")
    print("【已卖 Short Put】")
    if short_put_legs:
        print("标的      合约代码                  数量      行权价  到期日            成本")
        for position in sorted(short_put_legs, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    else:
        print("无")

    if other_short_legs:
        print("")
        print("【其他 Short Option】")
        print("标的      合约代码                  数量      行权价  到期日            成本")
        for position in sorted(other_short_legs, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))

    print("")
    print("【按标的汇总】")
    symbols = sorted({item.underlying for item in base_positions} | {item.underlying for item in short_calls})
    if symbols:
        grouped_bases = group_positions_by_underlying(base_positions)
        grouped_short_calls = group_positions_by_underlying(short_call_legs)
        grouped_short_puts = group_positions_by_underlying(short_put_legs)
        for symbol in symbols:
            base_count = sum(item.quantity for item in grouped_bases.get(symbol, []))
            short_call_count = sum(item.quantity for item in grouped_short_calls.get(symbol, []))
            short_put_count = sum(item.quantity for item in grouped_short_puts.get(symbol, []))
            available = max(base_count - short_call_count, 0)
            print(
                f"{symbol}: 底仓 {base_count} 张，已卖 short call {short_call_count} 张，"
                f"已卖 short put {short_put_count} 张，剩余可覆盖 short call {available} 张。"
            )
    else:
        print("无")
    print("====================================")


def load_position_memory_payload() -> Dict[str, Any]:
    return load_position_memory_payload_from_path(MEMORY_FILE)


def load_position_memory() -> Dict[str, str]:
    return load_position_memory_from_path(MEMORY_FILE)


def collect_positions_from_memory() -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    payload = load_position_memory_payload()
    memory = load_position_memory()
    base_raw = memory.get("base_positions", "")
    short_raw = memory.get("short_calls", "")
    base_positions = parse_positions_input(base_raw) if base_raw.strip() else []
    short_calls = parse_positions_input(short_raw) if short_raw.strip() else []
    return base_positions, short_calls, {
        "source": "memory",
        "memory_file": str(MEMORY_FILE),
        "memory_scope": safe_text(payload.get("memory_scope")) or "legacy_analysis_snapshot",
        "loaded": bool(base_positions or short_calls),
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_call_contracts": sum(item.quantity for item in short_calls),
    }


def save_position_memory(
    base_positions: List[PositionInput],
    short_calls: List[PositionInput],
    memory_scope: str = "analysis_snapshot",
) -> None:
    save_position_memory_to_path(MEMORY_FILE, base_positions, short_calls, memory_scope)


def load_broker_positions(path: Path) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    payload = read_json_object(path)
    base_positions = positions_from_records(payload.get("base_position_records"))
    short_calls = positions_from_records(payload.get("short_call_records"))
    return base_positions, short_calls, {
        "source": safe_text(payload.get("source")) or "broker_file",
        "path": str(path),
        "loaded": bool(base_positions or short_calls),
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_contracts": sum(item.quantity for item in short_calls),
    }


def save_broker_positions(path: Path, broker: str, base_positions: List[PositionInput], short_calls: List[PositionInput]) -> None:
    payload = {
        "source": broker,
        "base_positions": positions_to_compact_text(base_positions),
        "short_calls": positions_to_compact_text(short_calls),
        "base_position_records": [position_to_record(item) for item in base_positions],
        "short_call_records": [position_to_record(item) for item in short_calls],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_object(path, payload)


def combine_positions_by_code(*position_lists: List[PositionInput]) -> List[PositionInput]:
    combined: Dict[str, PositionInput] = {}
    for positions in position_lists:
        for position in positions:
            key = position.raw_code.upper()
            if key not in combined:
                combined[key] = position
                continue
            existing = combined[key]
            total_quantity = existing.quantity + position.quantity
            if total_quantity <= 0:
                combined.pop(key, None)
                continue
            if existing.cost_price is not None and position.cost_price is not None:
                cost_price = (existing.cost_price * existing.quantity + position.cost_price * position.quantity) / total_quantity
            elif existing.cost_price == position.cost_price:
                cost_price = existing.cost_price
            else:
                cost_price = None
            combined[key] = replace(existing, quantity=total_quantity, cost_price=cost_price)
    return list(combined.values())


def subtract_positions_by_code(current_positions: List[PositionInput], removals: List[PositionInput]) -> Tuple[List[PositionInput], List[Dict[str, Any]]]:
    remaining = {position.raw_code.upper(): position for position in combine_positions_by_code(current_positions)}
    unmatched: List[Dict[str, Any]] = []
    for removal in removals:
        key = removal.raw_code.upper()
        existing = remaining.get(key)
        if existing is None:
            unmatched.append({"code": removal.raw_code, "quantity": removal.quantity, "reason": "not_found"})
            continue
        new_quantity = existing.quantity - removal.quantity
        if new_quantity < 0:
            unmatched.append({"code": removal.raw_code, "quantity": removal.quantity, "available": existing.quantity, "reason": "quantity_exceeds_position"})
            remaining.pop(key, None)
        elif new_quantity == 0:
            remaining.pop(key, None)
        else:
            remaining[key] = replace(existing, quantity=new_quantity)
    return list(remaining.values()), unmatched


def diff_positions_by_code(previous_positions: List[PositionInput], current_positions: List[PositionInput]) -> Dict[str, Any]:
    previous = {position.raw_code.upper(): position for position in combine_positions_by_code(previous_positions)}
    current = {position.raw_code.upper(): position for position in combine_positions_by_code(current_positions)}
    opened_or_increased: List[Dict[str, Any]] = []
    closed_or_reduced: List[Dict[str, Any]] = []
    for key, current_position in current.items():
        previous_position = previous.get(key)
        previous_quantity = previous_position.quantity if previous_position is not None else 0
        if current_position.quantity > previous_quantity:
            record = position_to_record(current_position)
            record["quantity_change"] = current_position.quantity - previous_quantity
            opened_or_increased.append(record)
    for key, previous_position in previous.items():
        current_position = current.get(key)
        current_quantity = current_position.quantity if current_position is not None else 0
        if previous_position.quantity > current_quantity:
            record = position_to_record(previous_position)
            record["quantity_change"] = current_quantity - previous_position.quantity
            closed_or_reduced.append(record)
    return {
        "opened_or_increased": opened_or_increased,
        "closed_or_reduced": closed_or_reduced,
        "changed": bool(opened_or_increased or closed_or_reduced),
    }


def prompt_positions_with_memory(label: str, memory_value: str, allow_empty: bool = False) -> List[PositionInput]:
    return prompt_positions_with_memory_from_interaction(label, memory_value, allow_empty)


def collect_positions_interactive() -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    memory = load_position_memory()
    return collect_positions_interactive_from_interaction(memory)


def prompt_optional_positions(label: str) -> List[PositionInput]:
    return prompt_optional_positions_from_interaction(label)


def collect_external_positions_interactive() -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    return collect_external_positions_interactive_from_interaction()


def collect_external_positions_from_args(base_raw: str = "", short_raw: str = "") -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    base_positions = parse_positions_input(base_raw) if base_raw.strip() else []
    short_calls = parse_positions_input(short_raw) if short_raw.strip() else []
    return base_positions, short_calls, {
        "source": "external_args",
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_call_contracts": sum(item.quantity for item in short_calls),
    }


def print_broker_position_confirmation(
    broker_label: str,
    base_positions: List[PositionInput],
    short_positions: List[PositionInput],
    metadata: Dict[str, Any],
) -> None:
    short_call_legs = [item for item in short_positions if item.option_type == "CALL"]
    short_put_legs = [item for item in short_positions if item.option_type == "PUT"]
    print("")
    print(f"========== {broker_label} position record ==========")
    print(f"Source: {metadata.get('source', '-')}; file: {metadata.get('path', '-')}")
    print(
        f"Base calls: {sum(item.quantity for item in base_positions)}; "
        f"short calls: {sum(item.quantity for item in short_call_legs)}; "
        f"short puts: {sum(item.quantity for item in short_put_legs)}"
    )
    if base_positions:
        print("[Base calls]")
        for position in sorted(base_positions, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    if short_call_legs:
        print("[Short calls]")
        for position in sorted(short_call_legs, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    if short_put_legs:
        print("[Short puts]")
        for position in sorted(short_put_legs, key=lambda item: (item.underlying, item.expiry or "", item.strike or 0, item.raw_code)):
            print(format_position_inventory_line(position))
    if not base_positions and not short_positions:
        print("No saved Schwab option positions.")
    print("===================================================")


def apply_schwab_position_changes(
    schwab_base_positions: List[PositionInput],
    schwab_short_calls: List[PositionInput],
    add_base_positions: List[PositionInput],
    add_short_calls: List[PositionInput],
    remove_base_positions: List[PositionInput],
    remove_short_calls: List[PositionInput],
) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    updated_base = combine_positions_by_code(schwab_base_positions, add_base_positions)
    updated_short = combine_positions_by_code(schwab_short_calls, add_short_calls)
    updated_base, unmatched_base_removals = subtract_positions_by_code(updated_base, remove_base_positions)
    updated_short, unmatched_short_removals = subtract_positions_by_code(updated_short, remove_short_calls)
    return updated_base, updated_short, {
        "added_base_contracts": sum(item.quantity for item in add_base_positions),
        "added_short_contracts": sum(item.quantity for item in add_short_calls),
        "removed_base_contracts": sum(item.quantity for item in remove_base_positions),
        "removed_short_contracts": sum(item.quantity for item in remove_short_calls),
        "unmatched_base_removals": unmatched_base_removals,
        "unmatched_short_removals": unmatched_short_removals,
        "total_base_contracts": sum(item.quantity for item in updated_base),
        "total_short_contracts": sum(item.quantity for item in updated_short),
    }


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


def parse_tos_position_statement(path: Path) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
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


def position_memory_key(position: PositionInput) -> Tuple[str, Optional[float], Optional[str], Optional[str], Optional[float]]:
    return (
        position.raw_code.upper(),
        position.strike,
        position.expiry,
        position.option_type,
        position.cost_price,
    )


def reconcile_memory_positions_with_opend(
    memory_base_positions: List[PositionInput],
    memory_short_calls: List[PositionInput],
    opend_base_positions: List[PositionInput],
    opend_short_calls: List[PositionInput],
) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    opend_codes = {item.raw_code.upper() for item in opend_base_positions + opend_short_calls}
    memory_only_items: List[Tuple[str, PositionInput]] = []
    retained_base: List[PositionInput] = []
    retained_short: List[PositionInput] = []

    for position in memory_base_positions:
        if position.raw_code.upper() in opend_codes:
            continue
        memory_only_items.append(("base", position))
    for position in memory_short_calls:
        if position.raw_code.upper() in opend_codes:
            continue
        memory_only_items.append(("short", position))

    if not memory_only_items:
        return memory_base_positions, memory_short_calls, {
            "enabled": True,
            "memory_only_count": 0,
            "removed": [],
            "kept": [],
        }

    print("")
    print("========== 本地记忆仓位对账 ==========")
    print("下面这些仓位存在于 pmcc_last_positions.json，但本次 Futu OpenD 快照没有看到。")
    print("如果它们已经在 Futu 或其他券商平仓，请输入编号移除；如果仍是非 Futu 持仓，直接回车保留。")
    print("输入示例：B1,S2；输入 ALL 可全部移除。")
    index_by_id: Dict[str, Tuple[str, PositionInput]] = {}
    for index, (bucket, position) in enumerate(memory_only_items, start=1):
        prefix = "B" if bucket == "base" else "S"
        item_id = f"{prefix}{index}"
        index_by_id[item_id] = (bucket, position)
        label = "LEAPS/base" if bucket == "base" else "short option"
        print(f"{item_id}: {label}  {format_position_inventory_line(position)}")

    try:
        raw = read_interactive_text("要从本地记忆中移除的编号（回车=全部保留）: ")
    except EOFError:
        raw = ""

    remove_ids: set[str] = set()
    if raw.upper() == "ALL":
        remove_ids = set(index_by_id)
    elif raw:
        for token in re.split(r"[,;，；\s]+", raw.upper()):
            if token:
                if token in index_by_id:
                    remove_ids.add(token)
                else:
                    print(f"忽略无法识别的编号：{token}")

    removed_records: List[Dict[str, Any]] = []
    kept_records: List[Dict[str, Any]] = []
    remove_keys = {position_memory_key(index_by_id[item_id][1]) for item_id in remove_ids}

    for position in memory_base_positions:
        if position.raw_code.upper() in opend_codes:
            retained_base.append(position)
            continue
        if position_memory_key(position) in remove_keys:
            removed_records.append(position_to_record(position))
        else:
            retained_base.append(position)
            kept_records.append(position_to_record(position))

    for position in memory_short_calls:
        if position.raw_code.upper() in opend_codes:
            retained_short.append(position)
            continue
        if position_memory_key(position) in remove_keys:
            removed_records.append(position_to_record(position))
        else:
            retained_short.append(position)
            kept_records.append(position_to_record(position))

    print(f"本次从本地记忆移除 {len(removed_records)} 条；保留 memory-only 仓位 {len(kept_records)} 条。")
    print("====================================")
    return retained_base, retained_short, {
        "enabled": True,
        "memory_only_count": len(memory_only_items),
        "removed": removed_records,
        "kept": kept_records,
    }


def merge_positions_by_code(*sources: Tuple[str, List[PositionInput]]) -> Tuple[List[PositionInput], Dict[str, Any]]:
    merged: Dict[str, List[PositionInput]] = {}
    source_by_code: Dict[str, List[str]] = {}
    replacements: List[Dict[str, Any]] = []
    combined_duplicates: List[Dict[str, Any]] = []

    for source_name, positions in sources:
        for position in positions:
            key = position.raw_code.upper()
            if key in merged:
                previous_sources = source_by_code[key]
                if source_name == "opend" and previous_sources == ["memory"]:
                    replacements.append(
                        {
                            "code": position.raw_code,
                            "previous_sources": previous_sources,
                            "replacement_source": source_name,
                        }
                    )
                    merged[key] = [position]
                    source_by_code[key] = [source_name]
                    continue
                should_combine = (
                    (source_name == "external" and source_name not in previous_sources)
                    or (source_name == "opend" and "memory_external" in previous_sources)
                )
                if should_combine:
                    existing_positions = merged[key]
                    total_quantity = sum(item.quantity for item in existing_positions) + position.quantity
                    cost_inputs = existing_positions + [position]
                    if all(item.cost_price is not None for item in cost_inputs):
                        cost_price = sum(item.cost_price * item.quantity for item in cost_inputs if item.cost_price is not None) / total_quantity
                    else:
                        cost_price = None
                    merged[key] = [replace(existing_positions[0], quantity=total_quantity, cost_price=cost_price)]
                    combined_duplicates.append(
                        {
                            "code": position.raw_code,
                            "previous_sources": previous_sources,
                            "added_source": source_name,
                            "quantity": total_quantity,
                        }
                    )
                else:
                    merged[key].append(position)
            else:
                merged[key] = [position]
                source_by_code[key] = []
            if source_name not in source_by_code[key]:
                source_by_code[key].append(source_name)

    merged_positions = [position for positions in merged.values() for position in positions]
    return merged_positions, {
        "sources": sorted({source for sources in source_by_code.values() for source in sources}),
        "unique_contract_codes": len(merged),
        "replacements": replacements,
        "combined_duplicates": combined_duplicates,
    }


def collect_positions_from_opend(trd_env: str = TrdEnv.REAL, acc_index: Optional[int] = None) -> Tuple[List[PositionInput], List[PositionInput], Dict[str, Any]]:
    return collect_positions_from_opend_from_futu(HOST, PORT, trd_env=trd_env, acc_index=acc_index)


def enrich_error_message(message: str) -> str:
    if "https://" in message and ("问卷" in message or "协议" in message):
        return message + " Hint: complete the required Futu questionnaire/agreement first."
    if "无权限" in message or "权限" in message:
        return message + " Hint: check the US market quote permission on the Futu account connected to OpenD."
    return message


def estimate_chain_iv_rank_proxy(options: pd.DataFrame, stock_price: float, target_dte: int = 30) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "value": None,
        "status": "UNAVAILABLE",
        "method": "ATM/DTE-filtered chain IV cross-section; excluded from trading decision",
        "contracts_before_filter": int(len(options)) if options is not None else 0,
        "contracts_after_filter": 0,
        "contracts_after_outlier_filter": 0,
        "outliers_removed": 0,
        "min_required_contracts": 10,
        "target_dte": target_dte,
        "dte_window": [20, 45],
        "moneyness_window_pct": [-15, 15],
    }
    if options is None or options.empty:
        metadata["reason"] = "empty_option_chain"
        return metadata

    required = {"implied_volatility", "days_to_expiry", "strike_price"}
    missing = sorted(required - set(options.columns))
    if missing:
        metadata["reason"] = "missing_columns:" + ",".join(missing)
        return metadata

    working = options.copy()
    working["iv_numeric"] = pd.to_numeric(working["implied_volatility"], errors="coerce")
    working["dte_numeric"] = pd.to_numeric(working["days_to_expiry"], errors="coerce")
    working["strike_numeric"] = pd.to_numeric(working["strike_price"], errors="coerce")
    working = working.dropna(subset=["iv_numeric", "dte_numeric", "strike_numeric"])
    working = working[(working["iv_numeric"] > 0) & working["iv_numeric"].apply(math.isfinite)]
    if working.empty or stock_price <= 0:
        metadata["reason"] = "no_valid_iv_rows"
        return metadata

    working["moneyness_pct"] = (working["strike_numeric"] - stock_price) / stock_price * 100
    filtered = working[
        working["dte_numeric"].between(20, 45, inclusive="both")
        & working["moneyness_pct"].between(-15, 15, inclusive="both")
    ].copy()
    if filtered.empty:
        working["dte_distance"] = (working["dte_numeric"] - target_dte).abs()
        working["atm_distance_pct"] = working["moneyness_pct"].abs()
        filtered = working.sort_values(by=["dte_distance", "atm_distance_pct"]).head(20).copy()
        metadata["status"] = "FALLBACK_NEAREST"

    metadata["contracts_after_filter"] = int(len(filtered))
    median_iv = safe_float(filtered["iv_numeric"].median()) if not filtered.empty else None
    if median_iv is None or median_iv <= 0:
        metadata["reason"] = "median_iv_unavailable"
        return metadata

    cleaned = filtered[
        (filtered["iv_numeric"] <= median_iv * 3)
        & (filtered["iv_numeric"] >= median_iv / 3)
    ].copy()
    metadata["contracts_after_outlier_filter"] = int(len(cleaned))
    metadata["outliers_removed"] = int(len(filtered) - len(cleaned))
    metadata["median_iv"] = round(median_iv, 4)

    if len(cleaned) < metadata["min_required_contracts"]:
        metadata["status"] = "THIN_SAMPLE"
        metadata["reason"] = f"only_{len(cleaned)}_contracts_after_filter"
        return metadata

    iv_min = safe_float(cleaned["iv_numeric"].min())
    iv_max = safe_float(cleaned["iv_numeric"].max())
    iv_now = safe_float(cleaned["iv_numeric"].median())
    if iv_min is None or iv_max is None or iv_now is None:
        metadata["reason"] = "iv_range_unavailable"
        return metadata
    if iv_max == iv_min:
        proxy = 0.0
    else:
        proxy = max(0.0, min(100.0, (iv_now - iv_min) / (iv_max - iv_min) * 100))

    metadata.update(
        {
            "value": round(proxy, 2),
            "status": "OK" if metadata.get("status") != "FALLBACK_NEAREST" else "FALLBACK_NEAREST",
            "iv_min": round(iv_min, 4),
            "iv_max": round(iv_max, 4),
            "iv_median": round(iv_now, 4),
        }
    )
    if metadata["outliers_removed"]:
        metadata["status"] = "OUTLIER_FILTERED" if metadata["status"] == "OK" else metadata["status"]
    return metadata


def calculate_historical_iv_rank(current_iv: Optional[float], historical_ivs: List[float]) -> Optional[float]:
    if current_iv is None:
        return None
    values = [value for value in historical_ivs if value is not None and math.isfinite(value)]
    if len(values) < 20:
        return None
    iv_min = min(values)
    iv_max = max(values)
    if iv_max == iv_min:
        return 0.0
    return round(max(0.0, min(100.0, (current_iv - iv_min) / (iv_max - iv_min) * 100)), 2)


def calculate_historical_iv_percentile(current_iv: Optional[float], historical_ivs: List[float]) -> Optional[float]:
    if current_iv is None:
        return None
    values = sorted(value for value in historical_ivs if value is not None and math.isfinite(value))
    if len(values) < 20:
        return None
    count_at_or_below = sum(1 for value in values if value <= current_iv)
    return round(count_at_or_below / len(values) * 100, 2)


def extract_iv_history_value(item: Any) -> Optional[float]:
    if isinstance(item, dict):
        for key in ["iv", "pmcc_iv", "iv_30d_atm_mid", "iv_30d_atm_call", "implied_volatility", "value"]:
            value = safe_float(item.get(key))
            if value is not None:
                return value
        return None
    return safe_float(item)


def parse_history_date(value: Any) -> Optional[date]:
    text = safe_text(value)
    if text is None:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def normalize_iv_history_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"schema_version": 1, "records": {}}
    records = payload.get("records")
    if isinstance(records, dict):
        return {
            **payload,
            "schema_version": payload.get("schema_version", 1),
            "records": records,
        }

    legacy_records: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"schema_version", "updated_at", "records"}:
            continue
        if isinstance(value, (dict, list)):
            legacy_records[str(key).upper()] = value
    return {
        "schema_version": payload.get("schema_version", 1),
        "updated_at": payload.get("updated_at"),
        "records": legacy_records,
    }


def load_iv_history_payload() -> Dict[str, Any]:
    payload = read_json_object(IV_HISTORY_FILE)
    if not payload:
        return {"schema_version": 1, "records": {}}
    return normalize_iv_history_payload(payload)


def iv_history_entries_for_symbol(payload: Dict[str, Any], symbol: str) -> Tuple[str, List[Any]]:
    records = payload.get("records") if isinstance(payload, dict) else {}
    if not isinstance(records, dict):
        return "pmcc_iv_history_json", []

    symbol_key = symbol.upper()
    ticker_key = symbol_to_web_ticker(symbol)
    record = records.get(symbol_key) or records.get(ticker_key)
    source_name = "pmcc_iv_history_json"
    values_raw: Any = []
    if isinstance(record, dict):
        values_raw = record.get("history") or record.get("values") or record.get("iv_history") or []
        source_name = safe_text(record.get("source")) or source_name
    elif isinstance(record, list):
        values_raw = record

    if not isinstance(values_raw, list):
        values_raw = []
    return source_name, values_raw


def load_historical_iv_values(symbol: str) -> Dict[str, Any]:
    if not IV_HISTORY_FILE.exists():
        return {
            "source": "pmcc_iv_history_json",
            "status": "MISSING",
            "path": str(IV_HISTORY_FILE),
            "values": [],
        }
    try:
        payload = normalize_iv_history_payload(json.loads(IV_HISTORY_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "source": "pmcc_iv_history_json",
            "status": "ERROR",
            "path": str(IV_HISTORY_FILE),
            "error": str(exc),
            "values": [],
        }

    source_name, values_raw = iv_history_entries_for_symbol(payload, symbol)
    dated_values: List[Tuple[date, float]] = []
    undated_values: List[float] = []
    for item in values_raw:
        value = extract_iv_history_value(item)
        if value is None or not math.isfinite(value):
            continue
        if isinstance(item, dict):
            entry_date = parse_history_date(item.get("date") or item.get("updated_at") or item.get("timestamp"))
            if entry_date is not None:
                dated_values.append((entry_date, value))
                continue
        undated_values.append(value)

    values: List[float]
    if dated_values:
        dated_values = sorted(dated_values, key=lambda item: item[0])
        values = undated_values + [value for _, value in dated_values]
    else:
        values = undated_values
    if len(values) > IV_HISTORY_LOOKBACK_LIMIT:
        values = values[-IV_HISTORY_LOOKBACK_LIMIT:]

    return {
        "source": source_name,
        "status": "OK" if values else "NO_SYMBOL_HISTORY",
        "path": str(IV_HISTORY_FILE),
        "lookback_count": len(values),
        "lookback_limit": IV_HISTORY_LOOKBACK_LIMIT,
        "history_start_date": dated_values[max(0, len(dated_values) - min(len(values), len(dated_values)))][0].isoformat()
        if dated_values and values
        else None,
        "history_end_date": dated_values[-1][0].isoformat() if dated_values and values else None,
        "values": values,
    }


def select_atm_iv_sample(
    options: pd.DataFrame,
    stock_price: float,
    target_dte: int = 30,
    label: str = "option",
) -> Dict[str, Any]:
    sample: Dict[str, Any] = {
        "label": label,
        "iv": None,
        "status": "UNAVAILABLE",
        "target_dte": target_dte,
        "contracts_before_filter": int(len(options)) if options is not None else 0,
        "contracts_used": 0,
    }
    if options is None or options.empty or stock_price <= 0:
        sample["reason"] = "empty_options_or_bad_stock_price"
        return sample

    required = {"implied_volatility", "days_to_expiry", "strike_price"}
    missing = sorted(required - set(options.columns))
    if missing:
        sample["reason"] = "missing_columns:" + ",".join(missing)
        return sample

    working = options.copy()
    working["iv_numeric"] = pd.to_numeric(working["implied_volatility"], errors="coerce")
    working["dte_numeric"] = pd.to_numeric(working["days_to_expiry"], errors="coerce")
    working["strike_numeric"] = pd.to_numeric(working["strike_price"], errors="coerce")
    working = working.dropna(subset=["iv_numeric", "dte_numeric", "strike_numeric"])
    working = working[(working["iv_numeric"] > 0) & working["iv_numeric"].apply(math.isfinite)]
    if working.empty:
        sample["reason"] = "no_valid_iv_rows"
        return sample

    working["moneyness_abs_pct"] = ((working["strike_numeric"] - stock_price) / stock_price * 100).abs()
    working["dte_distance"] = (working["dte_numeric"] - target_dte).abs()
    filtered = working[
        working["dte_numeric"].between(20, 45, inclusive="both")
        & working["moneyness_abs_pct"].le(10)
    ].copy()
    status = "OK"
    if filtered.empty:
        filtered = working.sort_values(by=["dte_distance", "moneyness_abs_pct"]).head(8).copy()
        status = "FALLBACK_NEAREST"

    selected = filtered.sort_values(by=["dte_distance", "moneyness_abs_pct", "strike_numeric"]).head(4)
    if selected.empty:
        sample["reason"] = "no_contracts_after_filter"
        return sample

    first = selected.iloc[0]
    sample.update(
        {
            "iv": round(float(selected["iv_numeric"].median()), 4),
            "status": status,
            "contracts_after_filter": int(len(filtered)),
            "contracts_used": int(len(selected)),
            "nearest_code": safe_text(first.get("code")),
            "nearest_expiry": safe_text(first.get("strike_time")),
            "nearest_dte": safe_int(first.get("dte_numeric")),
            "nearest_strike": safe_float(first.get("strike_numeric")),
            "nearest_moneyness_abs_pct": round(float(first.get("moneyness_abs_pct")), 3)
            if safe_float(first.get("moneyness_abs_pct")) is not None
            else None,
        }
    )
    return sample


def estimate_pmcc_iv_snapshot(
    call_options: pd.DataFrame,
    stock_price: float,
    put_options: Optional[pd.DataFrame] = None,
    target_dte: int = 30,
) -> Dict[str, Any]:
    call_sample = select_atm_iv_sample(call_options, stock_price, target_dte, "call")
    put_sample = select_atm_iv_sample(put_options if put_options is not None else pd.DataFrame(), stock_price, target_dte, "put")
    call_iv = safe_float(call_sample.get("iv"))
    put_iv = safe_float(put_sample.get("iv"))

    iv_values = [value for value in [call_iv, put_iv] if value is not None]
    current_iv = round(sum(iv_values) / len(iv_values), 4) if iv_values else None
    if call_iv is not None and put_iv is not None:
        method = "30D ATM call/put midpoint IV sampled from Futu option chain"
    elif call_iv is not None:
        method = "30D ATM call-side IV sampled from Futu option chain"
    elif put_iv is not None:
        method = "30D ATM put-side IV sampled from Futu option chain"
    else:
        method = "PMCC IV sample unavailable"

    return {
        "iv": current_iv,
        "iv_30d_atm_mid": current_iv,
        "iv_30d_atm_call": call_iv,
        "iv_30d_atm_put": put_iv,
        "status": "OK" if current_iv is not None else "UNAVAILABLE",
        "method": method,
        "target_dte": target_dte,
        "call_sample": call_sample,
        "put_sample": put_sample,
        "history_file": str(IV_HISTORY_FILE),
    }


def record_pmcc_iv_history(symbol: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    current_iv = safe_float(snapshot.get("iv"))
    if current_iv is None:
        return {
            "status": "SKIPPED",
            "reason": "current_iv_unavailable",
            "path": str(IV_HISTORY_FILE),
        }

    payload = load_iv_history_payload()
    records = payload.setdefault("records", {})
    if not isinstance(records, dict):
        records = {}
        payload["records"] = records

    symbol_key = symbol.upper()
    source_name, existing_raw = iv_history_entries_for_symbol(payload, symbol)
    entries: List[Dict[str, Any]] = []
    for index, item in enumerate(existing_raw):
        if isinstance(item, dict):
            entries.append(item)
            continue
        legacy_value = extract_iv_history_value(item)
        if legacy_value is not None:
            entries.append({"iv": legacy_value, "source": "legacy_undated", "legacy_index": index})
    today_text = date.today().isoformat()
    entries = [item for item in entries if safe_text(item.get("date")) != today_text]
    entry = {
        "date": today_text,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol_key,
        "iv": round(current_iv, 4),
        "iv_30d_atm_mid": safe_float(snapshot.get("iv_30d_atm_mid")),
        "iv_30d_atm_call": safe_float(snapshot.get("iv_30d_atm_call")),
        "iv_30d_atm_put": safe_float(snapshot.get("iv_30d_atm_put")),
        "method": snapshot.get("method"),
        "target_dte": snapshot.get("target_dte"),
        "call_sample": snapshot.get("call_sample"),
        "put_sample": snapshot.get("put_sample"),
    }
    entries.append(entry)
    entries = sorted(entries, key=lambda item: safe_text(item.get("date")) or "")[-IV_HISTORY_MAX_RECORDS_PER_SYMBOL:]
    records[symbol_key] = {
        "source": "pmcc_auto_30d_atm_iv_history",
        "method": "Daily fixed PMCC IV sample; IV Rank uses the last 252 records.",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "history": entries,
    }
    payload.update(
        {
            "schema_version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    write_json_object(IV_HISTORY_FILE, payload)
    return {
        "status": "OK",
        "path": str(IV_HISTORY_FILE),
        "source_before_write": source_name,
        "symbol": symbol_key,
        "date": today_text,
        "iv": round(current_iv, 4),
        "records_for_symbol": len(entries),
    }


def parse_iv_rank_overrides(raw: str) -> Dict[str, float]:
    overrides: Dict[str, float] = {}
    for entry in re.split(r"[;,]", raw or ""):
        text = entry.strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"Cannot parse IV override '{text}'. Use VALUE for all symbols, or SYMBOL=VALUE, e.g. US.NVDA=63.")
        symbol, value = text.split("=", 1)
        parsed = safe_float(value)
        if parsed is None:
            raise ValueError(f"Cannot parse IV Rank value for '{symbol}'.")
        overrides[symbol.strip().upper()] = parsed
    return overrides


def parse_iv_override_input(raw: str, label: str) -> Tuple[Optional[float], Dict[str, float]]:
    text = (raw or "").strip()
    if not text:
        return None, {}
    if "=" not in text and not re.search(r"[;,]", text):
        parsed = safe_float(text)
        if parsed is None:
            raise ValueError(f"Cannot parse {label} override '{text}'. Use VALUE or SYMBOL=VALUE.")
        return parsed, {}
    return None, parse_iv_rank_overrides(text)


def symbol_iv_rank_override(symbol: str, config: StrategyConfig) -> Optional[float]:
    if config.iv_rank_override is not None:
        return config.iv_rank_override
    overrides = config.iv_rank_overrides or {}
    symbol_key = symbol.upper()
    ticker_key = symbol_to_web_ticker(symbol)
    return overrides.get(symbol_key) if symbol_key in overrides else overrides.get(ticker_key)


def symbol_iv_percentile_override(symbol: str, config: StrategyConfig) -> Optional[float]:
    if config.iv_percentile_override is not None:
        return config.iv_percentile_override
    overrides = config.iv_percentile_overrides or {}
    symbol_key = symbol.upper()
    ticker_key = symbol_to_web_ticker(symbol)
    return overrides.get(symbol_key) if symbol_key in overrides else overrides.get(ticker_key)


def load_iv_rank_memory() -> Dict[str, Dict[str, Any]]:
    payload = read_json_object(IV_RANK_MEMORY_FILE)
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, dict):
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for symbol, record in records.items():
        if isinstance(record, dict):
            value = safe_float(record.get("iv_rank"))
            updated_at = safe_text(record.get("updated_at"))
            source = safe_text(record.get("source")) or "local_memory"
        else:
            value = safe_float(record)
            updated_at = None
            source = "local_memory"
        if value is not None:
            normalized[symbol.upper()] = {
                "iv_rank": value,
                "updated_at": updated_at,
                "source": source,
            }
    return normalized


def save_iv_rank_memory(records: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    write_json_object(IV_RANK_MEMORY_FILE, payload)


def remember_iv_rank_value(
    symbol: str,
    value: Optional[float],
    source: str,
    iv_percentile: Optional[float] = None,
) -> None:
    parsed = safe_float(value)
    if parsed is None:
        return
    parsed_percentile = safe_float(iv_percentile)
    memory = load_iv_rank_memory()
    record = {
        "iv_rank": parsed,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
    }
    if parsed_percentile is not None:
        record["iv_percentile"] = parsed_percentile
    memory[symbol.upper()] = record
    save_iv_rank_memory(memory)


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = safe_text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def fresh_iv_rank_memory_record(symbol: str) -> Optional[Dict[str, Any]]:
    memory = load_iv_rank_memory()
    symbol_key = symbol.upper()
    ticker_key = symbol_to_web_ticker(symbol)
    record = memory.get(symbol_key) or memory.get(ticker_key)
    if not record:
        return None
    value = safe_float(record.get("iv_rank"))
    if value is None:
        return None
    percentile = safe_float(record.get("iv_percentile"))
    updated_at = parse_iso_datetime(record.get("updated_at"))
    if updated_at is None:
        return None
    age_days = (datetime.now() - updated_at).total_seconds() / 86400
    if age_days > IV_RANK_MEMORY_MAX_AGE_DAYS:
        return None
    return {
        **record,
        "iv_rank": value,
        "iv_percentile": percentile,
        "age_days": round(age_days, 3),
        "max_age_days": IV_RANK_MEMORY_MAX_AGE_DAYS,
    }


def build_iv_rank_input_metadata(symbols: List[str], config: StrategyConfig) -> Dict[str, Any]:
    memory = load_iv_rank_memory()
    metadata: Dict[str, Any] = {
        "memory_file": str(IV_RANK_MEMORY_FILE),
        "prompted": False,
        "symbols": {},
    }
    for symbol in symbols:
        symbol_key = symbol.upper()
        ticker_key = symbol_to_web_ticker(symbol)
        explicit_value = symbol_iv_rank_override(symbol, config)
        explicit_percentile = symbol_iv_percentile_override(symbol, config)
        memory_record = memory.get(symbol_key) or memory.get(ticker_key)
        fresh_record = fresh_iv_rank_memory_record(symbol)
        metadata["symbols"][symbol_key] = {
            "cli_override": explicit_value,
            "cli_percentile_override": explicit_percentile,
            "memory_value": safe_float(memory_record.get("iv_rank")) if memory_record else None,
            "memory_percentile": safe_float(memory_record.get("iv_percentile")) if memory_record else None,
            "memory_updated_at": memory_record.get("updated_at") if memory_record else None,
            "memory_source": memory_record.get("source") if memory_record else None,
            "memory_fresh_for_fallback": fresh_record is not None,
            "note": "Manual memory is only a same-day fallback; automatic PMCC IV history has priority.",
        }
    return metadata


def prompt_iv_rank_value(symbol: str, current_value: Optional[float]) -> Optional[float]:
    from pmcc.interaction import prompt_iv_rank_value as prompt_iv_rank_value_from_interaction

    return prompt_iv_rank_value_from_interaction(symbol, current_value)


def collect_iv_rank_overrides_for_symbols(
    symbols: List[str],
    config: StrategyConfig,
) -> Tuple[StrategyConfig, Dict[str, Any]]:
    memory = load_iv_rank_memory()
    overrides, memory, metadata, changed = collect_iv_rank_overrides_for_symbols_from_interaction(
        symbols,
        dict(config.iv_rank_overrides or {}),
        memory,
        str(IV_RANK_MEMORY_FILE),
        lambda symbol: symbol_iv_rank_override(symbol, config),
        datetime.now().isoformat(timespec="seconds"),
    )
    if changed:
        save_iv_rank_memory(memory)

    return replace(config, iv_rank_overrides=overrides), metadata


def build_iv_rank_analysis(
    symbol: str,
    current_iv: Optional[float],
    chain_iv_proxy: Optional[float],
    chain_iv_proxy_meta: Optional[Dict[str, Any]],
    config: StrategyConfig,
) -> Dict[str, Any]:
    override = symbol_iv_rank_override(symbol, config)
    percentile_override = symbol_iv_percentile_override(symbol, config)
    if override is not None:
        return {
            "iv_rank": round(override, 2),
            "iv_percentile": percentile_override,
            "current_iv": current_iv,
            "source": "cli_manual_iv_rank_override",
            "method": "explicit user supplied historical IV Rank override",
            "chain_iv_rank_proxy": chain_iv_proxy,
            "chain_iv_rank_proxy_meta": chain_iv_proxy_meta,
            "is_true_historical_iv_rank": True,
            "is_decision_usable_iv_rank": True,
            "priority": "cli_override",
        }

    history = load_historical_iv_values(symbol)
    values = history.get("values") or []
    iv_rank = calculate_historical_iv_rank(current_iv, values)
    iv_percentile = calculate_historical_iv_percentile(current_iv, values)
    if iv_rank is not None:
        return {
            "iv_rank": iv_rank,
            "iv_percentile": iv_percentile,
            "current_iv": current_iv,
            "source": history.get("source"),
            "method": "current IV position within historical IV min/max range",
            "lookback_count": history.get("lookback_count"),
            "history_status": history.get("status"),
            "history_start_date": history.get("history_start_date"),
            "history_end_date": history.get("history_end_date"),
            "lookback_limit": history.get("lookback_limit"),
            "chain_iv_rank_proxy": chain_iv_proxy,
            "chain_iv_rank_proxy_meta": chain_iv_proxy_meta,
            "is_true_historical_iv_rank": True,
            "is_decision_usable_iv_rank": True,
            "priority": "pmcc_auto_history",
        }

    memory_record = fresh_iv_rank_memory_record(symbol)
    if memory_record is not None:
        memory_percentile = percentile_override
        if memory_percentile is None:
            memory_percentile = safe_float(memory_record.get("iv_percentile"))
        return {
            "iv_rank": round(safe_float(memory_record.get("iv_rank")) or 0.0, 2),
            "iv_percentile": memory_percentile,
            "current_iv": current_iv,
            "source": "manual_iv_rank_memory_fallback",
            "method": "same-day user supplied IV Rank memory; automatic PMCC IV history unavailable or insufficient",
            "lookback_count": history.get("lookback_count", 0),
            "history_status": history.get("status"),
            "history_path": history.get("path"),
            "memory_updated_at": memory_record.get("updated_at"),
            "memory_source": memory_record.get("source"),
            "memory_age_days": memory_record.get("age_days"),
            "chain_iv_rank_proxy": chain_iv_proxy,
            "chain_iv_rank_proxy_meta": chain_iv_proxy_meta,
            "is_true_historical_iv_rank": False,
            "is_decision_usable_iv_rank": True,
            "priority": "fresh_manual_memory_fallback",
        }

    return {
        "iv_rank": None,
        "iv_percentile": None,
        "current_iv": current_iv,
        "source": history.get("source"),
        "method": "historical IV Rank unavailable; chain proxy excluded from trading decision",
        "lookback_count": history.get("lookback_count", 0),
        "history_status": history.get("status"),
        "history_path": history.get("path"),
        "chain_iv_rank_proxy": chain_iv_proxy,
        "chain_iv_rank_proxy_meta": chain_iv_proxy_meta,
        "is_true_historical_iv_rank": False,
        "is_decision_usable_iv_rank": False,
        "priority": "unavailable",
    }


def estimate_underlying_iv(options: pd.DataFrame, stock_price: float, target_dte: int = 30) -> Optional[float]:
    if options.empty or "implied_volatility" not in options.columns:
        return None
    working = options.copy()
    working["iv_numeric"] = pd.to_numeric(working["implied_volatility"], errors="coerce")
    working["dte_numeric"] = pd.to_numeric(working["days_to_expiry"], errors="coerce")
    working["strike_numeric"] = pd.to_numeric(working["strike_price"], errors="coerce")
    working = working.dropna(subset=["iv_numeric", "dte_numeric", "strike_numeric"])
    if working.empty:
        return None
    working["atm_distance"] = (working["strike_numeric"] - stock_price).abs()
    working["dte_distance"] = (working["dte_numeric"] - target_dte).abs()
    row = working.sort_values(by=["dte_distance", "atm_distance"]).iloc[0]
    return round(float(row["iv_numeric"]), 2)


def enrich_options(chain: pd.DataFrame, greeks: pd.DataFrame, stock_price: float) -> pd.DataFrame:
    merged = pd.merge(chain, greeks, on="code", how="inner")
    if merged.empty:
        return merged

    if "delta" not in merged.columns:
        merged["delta"] = pd.NA
    if "implied_volatility" not in merged.columns:
        merged["implied_volatility"] = pd.NA

    for column in ["delta", "implied_volatility", "strike_price"]:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")

    merged["distance_from_spot"] = (merged["strike_price"] - stock_price).abs()
    merged["is_otm"] = merged["strike_price"] >= stock_price
    merged["otm_pct"] = (merged["strike_price"] - stock_price) / stock_price
    merged["expiry_date"] = merged["strike_time"].apply(parse_expiry) if "strike_time" in merged.columns else None
    if "strike_time" in merged.columns:
        today = pd.Timestamp.today().date()
        merged["days_to_expiry"] = merged["expiry_date"].apply(lambda value: (value - today).days if value else None)
    else:
        merged["days_to_expiry"] = None
    return merged


def apply_manual_overrides(
    enriched: pd.DataFrame,
    trend: str,
    iv_rank: Optional[float],
    iv_percentile: Optional[float],
    underlying_iv: Optional[float],
    hv: Optional[float],
    interactive: bool,
    config: StrategyConfig,
) -> Tuple[pd.DataFrame, str, Optional[float], Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
    manual_inputs: Dict[str, Any] = {}
    working = enriched.copy()
    greeks_available = bool(pd.to_numeric(working["delta"], errors="coerce").notna().any())

    if config.trend_override is not None:
        trend = config.trend_override
        manual_inputs["trend"] = config.trend_override

    if config.iv_rank_override is not None:
        iv_rank = config.iv_rank_override
        manual_inputs["iv_rank"] = config.iv_rank_override
        manual_inputs["iv_rank_source"] = "cli_override"

    symbol_for_override = None
    if not working.empty and "code" in working.columns:
        first_code = safe_text(working["code"].iloc[0])
        if first_code:
            symbol_for_override = parse_option_code_metadata(first_code).get("underlying")
    percentile_override = symbol_iv_percentile_override(symbol_for_override, config) if symbol_for_override else config.iv_percentile_override
    if percentile_override is not None:
        iv_percentile = percentile_override
        manual_inputs["iv_percentile"] = percentile_override
        manual_inputs["iv_percentile_source"] = "cli_override"

    if config.iv_override is not None:
        underlying_iv = config.iv_override
        manual_inputs["iv"] = config.iv_override
        manual_inputs["iv_source"] = "cli_override"

    if config.hv_override is not None:
        hv = config.hv_override
        manual_inputs["hv"] = config.hv_override
        manual_inputs["hv_source"] = "cli_override"

    if not interactive:
        return working, trend, iv_rank, iv_percentile, underlying_iv, hv, manual_inputs

    if trend == "UNKNOWN":
        print("趋势说明：")
        print("- UP: 偏上涨/偏强")
        print("- DOWN: 偏下跌/偏弱")
        print("- FLAT: 横盘/震荡")
        manual_trend = prompt_optional_trend("无法自动判断趋势，请手动输入趋势")
        if manual_trend is not None:
            trend = manual_trend
            manual_inputs["trend"] = manual_trend
    elif config.trend_override is None:
        print("趋势说明：")
        print("- UP: 偏上涨/偏强")
        print("- DOWN: 偏下跌/偏弱")
        print("- FLAT: 横盘/震荡")
        confirmed_trend = prompt_confirm_or_override_trend("请确认或覆盖趋势判断", trend)
        if confirmed_trend != trend:
            manual_inputs["trend"] = confirmed_trend
            manual_inputs["trend_source"] = "interactive_override"
        trend = confirmed_trend

    if iv_rank is None:
        print("IV Rank 说明：输入 0-100 的数字。")
        print("一般来说，数值越高表示当前隐波越高，卖方权利金环境越好。")
        print("示例：71 表示 IV Rank 约为 71。")
        manual_iv_rank = prompt_optional_float("无法自动获取 IV Rank，请手动输入 IV Rank")
        if manual_iv_rank is not None:
            iv_rank = manual_iv_rank
            manual_inputs["iv_rank"] = manual_iv_rank
            manual_inputs["iv_rank_source"] = "interactive_missing_data"
    elif config.iv_rank_override is None:
        print("IV Rank 说明：Futu 当前接口通常只能给当前 IV，程序估算值不等同于真实历史 IV Rank。")
        print("如果你有券商或其他平台的 IV Rank，请输入覆盖；否则直接回车沿用估算值。")
        confirmed_iv_rank = prompt_confirm_or_override_float("请确认或覆盖 IV Rank", iv_rank)
        if confirmed_iv_rank != iv_rank:
            manual_inputs["iv_rank"] = confirmed_iv_rank
            manual_inputs["iv_rank_source"] = "interactive_override"
        iv_rank = confirmed_iv_rank

    if percentile_override is None:
        print("IV Percentile 说明：如果你有券商显示的 IV Percentile，可输入覆盖；否则直接回车跳过。")
        manual_iv_percentile = prompt_confirm_or_override_float("请确认或输入 IV Percentile", iv_percentile)
        if manual_iv_percentile is not None:
            iv_percentile = manual_iv_percentile
            manual_inputs["iv_percentile"] = manual_iv_percentile
            manual_inputs["iv_percentile_source"] = "interactive_override"

    if config.iv_override is None:
        print("IV 说明：这是标的层面的 IV，可用程序从近 30D ATM 期权估算；如券商有更准确数值可输入覆盖。")
        manual_iv = prompt_confirm_or_override_float("请确认或输入标的 IV", underlying_iv)
        if manual_iv is not None:
            underlying_iv = manual_iv
            manual_inputs["iv"] = manual_iv
            manual_inputs["iv_source"] = "interactive_override"

    if config.hv_override is None:
        print("HV 说明：输入历史波动率百分比，例如 33.2 表示 HV 33.2%。")
        print("如果暂时没有 HV，直接回车跳过。")
        manual_hv = prompt_confirm_or_override_float("请确认或输入 HV", hv)
        if manual_hv is not None:
            hv = manual_hv
            manual_inputs["hv"] = manual_hv
            manual_inputs["hv_source"] = "interactive_override"

    return working, trend, iv_rank, iv_percentile, underlying_iv, hv, manual_inputs


def prompt_short_call_manual_metrics(short_call: PositionInput) -> Dict[str, Any]:
    return prompt_short_call_manual_metrics_from_interaction(short_call)


def build_pmcc_rulebook(config: StrategyConfig) -> Dict[str, Any]:
    return {
        "one_line_cn": "本程序以 PMCC 风险管理为核心，优先管理现有 short call 的 Delta、DTE、利润回收、IV 环境和 LEAPS 保护；新增卖出建议仅在同一标的覆盖额度允许时生效。",
        "coverage_scope": "per_underlying_only",
        "new_short_call": {
            "target_delta": [config.target_delta_low, config.target_delta_high],
            "target_dte": [config.preferred_dte_min, config.preferred_dte_max],
            "high_iv_rank": config.iv_rank_sell_threshold,
            "avoid_iv_rank_at_or_below": config.iv_rank_avoid_threshold,
        },
        "existing_short_call": {
            "delta": {
                "warn_delta": config.roll_delta_warn,
                "danger_delta": config.roll_delta_danger,
                "critical_delta": config.roll_delta_critical,
            },
            "dte": {
                "attention_dte_at_or_below": config.roll_dte_attention,
                "active_dte_below": config.roll_dte_active,
                "urgent_dte_below": config.roll_dte_urgent,
            },
            "profit_capture": {
                "profit_take": config.roll_profit_take,
                "strong_profit": config.roll_profit_strong,
            },
            "high_iv_rank": config.high_iv_roll_threshold,
        },
    }


def find_option_row(options: pd.DataFrame, option_code: str) -> Optional[pd.Series]:
    if options.empty:
        return None
    matched = options[options["code"].astype(str) == option_code]
    return None if matched.empty else matched.iloc[0]


def estimate_long_leg_metrics(
    base_positions: List[PositionInput],
    stock_price: float,
    options: pd.DataFrame,
) -> Dict[str, Any]:
    legs: List[Dict[str, Any]] = []
    warnings: List[str] = []
    total_quantity = 0
    weighted_delta = 0.0
    delta_quantity = 0
    dte_weighted_delta = 0.0
    dte_weight_total = 0.0
    dte_risk_weighted_delta = 0.0
    dte_risk_weight_total = 0.0
    net_long_gamma = 0.0
    net_long_vega = 0.0
    min_long_dte = None
    min_long_delta = None
    max_long_gamma = None
    safe_short_strikes: List[float] = []
    coverage_slots: List[Dict[str, Any]] = []
    expiry_buckets: Dict[str, Dict[str, Any]] = {}

    for item in base_positions:
        option_row = find_option_row(options, item.raw_code)
        strike = item.strike
        expiry = item.expiry
        dte = None
        delta = None
        gamma = None
        vega = None
        iv = None
        last_price = None
        bid_price = None
        ask_price = None

        if option_row is not None:
            strike = safe_float(option_row.get("strike_price")) or strike
            expiry = safe_text(option_row.get("strike_time")) or expiry
            dte = safe_int(option_row.get("days_to_expiry"))
            delta = safe_float(option_row.get("delta"))
            gamma = safe_float(option_row.get("gamma"))
            vega = safe_float(option_row.get("vega"))
            iv = safe_float(option_row.get("implied_volatility"))
            last_price = safe_float(option_row.get("last_price"))
            bid_price = safe_float(option_row.get("bid_price"))
            ask_price = safe_float(option_row.get("ask_price"))

        if dte is None and expiry is not None:
            expiry_date = parse_expiry(expiry)
            if expiry_date is not None:
                dte = (expiry_date - pd.Timestamp.today().date()).days

        mark_price = None
        if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price > 0:
            mark_price = (bid_price + ask_price) / 2
        elif last_price is not None and last_price > 0:
            mark_price = last_price

        basis_price = item.cost_price if item.cost_price is not None else mark_price
        intrinsic_value = max(stock_price - strike, 0) if strike is not None else None
        extrinsic_value = None
        extrinsic_pct = None
        if mark_price is not None and intrinsic_value is not None:
            extrinsic_value = max(mark_price - intrinsic_value, 0)
            if mark_price > 0:
                extrinsic_pct = extrinsic_value / mark_price

        safe_short_strike = None
        if strike is not None and basis_price is not None:
            safe_short_strike = strike + basis_price
            for _ in range(max(item.quantity, 0)):
                safe_short_strikes.append(safe_short_strike)
                coverage_slots.append({
                    "leaps_code": item.raw_code,
                    "leaps_strike": strike,
                    "leaps_expiry": expiry,
                    "leaps_cost": item.cost_price,
                    "leaps_iv": iv,
                    "minimum_safe_short_strike": safe_short_strike,
                })

        quality_flags: List[str] = []
        if delta is None:
            quality_flags.append("delta_unavailable")
        elif delta >= 0.90:
            quality_flags.append("stock_substitute_ideal")
        elif delta >= 0.80:
            quality_flags.append("stock_substitute_acceptable")
        else:
            quality_flags.append("delta_below_mcmillan_stock_substitute")
            warnings.append(f"{item.raw_code}: long-leg delta {delta:.3f} is below McMillan-style 0.80 stock-substitute threshold.")

        if extrinsic_pct is not None:
            if extrinsic_pct <= 0.15:
                quality_flags.append("low_extrinsic_value")
            else:
                quality_flags.append("high_extrinsic_value")
                warnings.append(f"{item.raw_code}: extrinsic value is {extrinsic_pct * 100:.1f}% of option mark, so the long leg has meaningful theta exposure.")

        if dte is not None and dte < 180:
            quality_flags.append("short_long_leg_dte")
            warnings.append(f"{item.raw_code}: long leg has only {dte} DTE, which is short for a LEAPS-style PMCC base.")

        quantity = max(item.quantity, 0)
        total_quantity += quantity
        bucket_key = expiry or "UNKNOWN"
        bucket = expiry_buckets.setdefault(
            bucket_key,
            {
                "expiry": expiry,
                "quantity": 0,
                "delta_sum": 0.0,
                "delta_quantity": 0,
                "net_gamma": 0.0,
                "net_vega": 0.0,
                "min_dte": None,
                "max_dte": None,
                "legs": [],
            },
        )
        bucket["quantity"] += quantity
        bucket["legs"].append(item.raw_code)
        if dte is not None:
            bucket["min_dte"] = dte if bucket["min_dte"] is None else min(bucket["min_dte"], dte)
            bucket["max_dte"] = dte if bucket["max_dte"] is None else max(bucket["max_dte"], dte)
            min_long_dte = dte if min_long_dte is None else min(min_long_dte, dte)
        if delta is not None:
            weighted_delta += delta * quantity
            delta_quantity += quantity
            bucket["delta_sum"] += delta * quantity
            bucket["delta_quantity"] += quantity
            min_long_delta = delta if min_long_delta is None else min(min_long_delta, delta)
            if dte is not None:
                dte_weight = quantity * max(dte, 0)
                dte_weighted_delta += delta * dte_weight
                dte_weight_total += dte_weight
                risk_weight = quantity * (365.0 / max(dte, 30))
                dte_risk_weighted_delta += delta * risk_weight
                dte_risk_weight_total += risk_weight
        if gamma is not None:
            net_long_gamma += gamma * quantity
            bucket["net_gamma"] += gamma * quantity
            max_long_gamma = gamma if max_long_gamma is None else max(max_long_gamma, gamma)
        if vega is not None:
            net_long_vega += vega * quantity
            bucket["net_vega"] += vega * quantity

        legs.append({
            "code": item.raw_code,
            "quantity": item.quantity,
            "strike": strike,
            "expiry": expiry,
            "days_to_expiry": dte,
            "delta": delta,
            "gamma": gamma,
            "vega": vega,
            "iv": iv,
            "mark_price": round(mark_price, 4) if mark_price is not None else None,
            "cost_price": item.cost_price,
            "intrinsic_value": round(intrinsic_value, 4) if intrinsic_value is not None else None,
            "extrinsic_value": round(extrinsic_value, 4) if extrinsic_value is not None else None,
            "extrinsic_pct": round(extrinsic_pct * 100, 2) if extrinsic_pct is not None else None,
            "minimum_safe_short_strike": round(safe_short_strike, 2) if safe_short_strike is not None else None,
            "quality_flags": quality_flags,
        })

    average_delta = weighted_delta / delta_quantity if delta_quantity else None
    time_weighted_delta = dte_weighted_delta / dte_weight_total if dte_weight_total else None
    risk_weighted_delta = dte_risk_weighted_delta / dte_risk_weight_total if dte_risk_weight_total else None
    expiry_bucket_rows = []
    for bucket in expiry_buckets.values():
        bucket_delta_quantity = safe_int(bucket.get("delta_quantity")) or 0
        bucket_delta_sum = safe_float(bucket.get("delta_sum")) or 0.0
        expiry_bucket_rows.append(
            {
                "expiry": bucket.get("expiry"),
                "quantity": bucket.get("quantity"),
                "min_dte": bucket.get("min_dte"),
                "max_dte": bucket.get("max_dte"),
                "average_delta": round(bucket_delta_sum / bucket_delta_quantity, 4) if bucket_delta_quantity else None,
                "net_delta": round(bucket_delta_sum, 4),
                "net_gamma": round(safe_float(bucket.get("net_gamma")) or 0.0, 6),
                "net_vega": round(safe_float(bucket.get("net_vega")) or 0.0, 4),
                "legs": bucket.get("legs") or [],
            }
        )
    expiry_bucket_rows = sorted(
        expiry_bucket_rows,
        key=lambda item: safe_int(item.get("min_dte")) if safe_int(item.get("min_dte")) is not None else 999999,
    )
    expiry_count = len({item.get("expiry") for item in expiry_bucket_rows if item.get("expiry")})
    if expiry_count > 1:
        expiry_labels = ", ".join(str(item.get("expiry")) for item in expiry_bucket_rows if item.get("expiry"))
        warnings.append(
            f"Long LEAPS use {expiry_count} different expiries ({expiry_labels}); average long-leg delta can hide expiry-bucket gamma and DTE risk."
        )
    return {
        "legs": legs,
        "total_quantity": total_quantity,
        "average_delta": round(average_delta, 4) if average_delta is not None else None,
        "net_long_delta": round(weighted_delta, 4),
        "time_weighted_delta": round(time_weighted_delta, 4) if time_weighted_delta is not None else None,
        "dte_risk_weighted_delta": round(risk_weighted_delta, 4) if risk_weighted_delta is not None else None,
        "net_long_gamma": round(net_long_gamma, 6),
        "net_long_vega": round(net_long_vega, 4),
        "min_long_dte": min_long_dte,
        "min_long_delta": round(min_long_delta, 4) if min_long_delta is not None else None,
        "max_long_gamma": round(max_long_gamma, 6) if max_long_gamma is not None else None,
        "expiry_bucket_count": expiry_count,
        "expiry_buckets": expiry_bucket_rows,
        "minimum_safe_short_strike_max": round(max(safe_short_strikes), 2) if safe_short_strikes else None,
        "coverage_slots": [
            {
                **slot,
                "slot": index + 1,
                "minimum_safe_short_strike": round(safe_float(slot.get("minimum_safe_short_strike")) or 0, 2),
                "leaps_strike": round(safe_float(slot.get("leaps_strike")) or 0, 2),
                "leaps_cost": round(safe_float(slot.get("leaps_cost")) or 0, 2),
                "leaps_iv": round(safe_float(slot.get("leaps_iv")), 4) if safe_float(slot.get("leaps_iv")) is not None else None,
            }
            for index, slot in enumerate(
                sorted(
                    coverage_slots,
                    key=lambda item: safe_float(item.get("minimum_safe_short_strike")) or float("inf"),
                )
            )
        ],
        "warnings": warnings,
    }


def build_leaps_coverage_slot_analysis(
    long_leg_analysis: Dict[str, Any],
    short_calls: List[PositionInput],
    candidate_strike: Optional[float],
) -> Dict[str, Any]:
    slots = [
        {
            **slot,
            "occupied_by": None,
            "eligible_for_candidate": False,
        }
        for slot in (long_leg_analysis.get("coverage_slots") or [])
    ]
    slots = sorted(slots, key=lambda item: safe_float(item.get("minimum_safe_short_strike")) or float("inf"))

    for short_call in sorted(short_calls, key=lambda item: item.strike or float("inf")):
        for _ in range(max(short_call.quantity, 0)):
            strike = short_call.strike
            open_slot = None
            if strike is not None:
                for slot in slots:
                    if slot.get("occupied_by") is None and safe_float(slot.get("minimum_safe_short_strike")) is not None and safe_float(slot.get("minimum_safe_short_strike")) <= strike:
                        open_slot = slot
                        break
            if open_slot is None:
                open_slot = next((slot for slot in slots if slot.get("occupied_by") is None), None)
            if open_slot is not None:
                open_slot["occupied_by"] = short_call.raw_code

    eligible_slots = []
    if candidate_strike is not None:
        for slot in slots:
            minimum_safe = safe_float(slot.get("minimum_safe_short_strike"))
            eligible = slot.get("occupied_by") is None and minimum_safe is not None and candidate_strike >= minimum_safe
            slot["eligible_for_candidate"] = eligible
            if eligible:
                eligible_slots.append(slot)

    blocked_slots = [
        slot for slot in slots
        if slot.get("occupied_by") is None and not slot.get("eligible_for_candidate")
    ]
    return {
        "slots": slots,
        "total_slots": len(slots),
        "occupied_slots": sum(1 for slot in slots if slot.get("occupied_by")),
        "eligible_new_short_call_slots": len(eligible_slots),
        "blocked_unoccupied_slots": len(blocked_slots),
        "candidate_strike": candidate_strike,
        "minimum_candidate_safe_strike": min(
            (safe_float(slot.get("minimum_safe_short_strike")) for slot in slots if slot.get("occupied_by") is None and safe_float(slot.get("minimum_safe_short_strike")) is not None),
            default=None,
        ),
        "max_unoccupied_safe_strike": max(
            (safe_float(slot.get("minimum_safe_short_strike")) for slot in slots if slot.get("occupied_by") is None and safe_float(slot.get("minimum_safe_short_strike")) is not None),
            default=None,
        ),
    }


def enrich_leaps_slot_iv_spreads(
    slot_analysis: Dict[str, Any],
    short_call_reviews: List[Dict[str, Any]],
    candidate_short_call: Dict[str, Any],
) -> Dict[str, Any]:
    if not slot_analysis:
        return slot_analysis

    reviews_by_code = {
        str(item.get("code")): item
        for item in (short_call_reviews or [])
        if item.get("code")
    }
    candidate_iv = safe_float(candidate_short_call.get("iv")) or safe_float(candidate_short_call.get("implied_volatility"))
    candidate_code = candidate_short_call.get("code")

    slots: List[Dict[str, Any]] = []
    existing_spreads: List[float] = []
    candidate_spreads: List[float] = []
    missing_iv_slots = 0

    for raw_slot in slot_analysis.get("slots") or []:
        slot = dict(raw_slot)
        long_iv = safe_float(slot.get("leaps_iv"))
        short_iv = None
        paired_code = None
        pair_type = "unpaired"

        occupied_code = slot.get("occupied_by")
        if occupied_code:
            paired_code = occupied_code
            review = reviews_by_code.get(str(occupied_code)) or {}
            short_iv = safe_float(review.get("iv")) or safe_float(review.get("implied_volatility"))
            pair_type = "existing_short_call"
        elif slot.get("eligible_for_candidate") and candidate_iv is not None:
            paired_code = candidate_code
            short_iv = candidate_iv
            pair_type = "candidate_short_call"

        spread = None
        if short_iv is not None and long_iv is not None:
            spread = round(short_iv - long_iv, 4)
            if pair_type == "existing_short_call":
                existing_spreads.append(spread)
            elif pair_type == "candidate_short_call":
                candidate_spreads.append(spread)
        elif pair_type != "unpaired":
            missing_iv_slots += 1

        slot.update(
            {
                "iv_pair_type": pair_type,
                "paired_short_code": paired_code,
                "paired_short_iv": round(short_iv, 4) if short_iv is not None else None,
                "short_long_iv_spread": spread,
            }
        )
        slots.append(slot)

    enriched = {**slot_analysis, "slots": slots}
    enriched.update(
        {
            "existing_short_long_iv_spreads": existing_spreads,
            "candidate_short_long_iv_spreads": candidate_spreads,
            "average_existing_short_long_iv_spread": round(sum(existing_spreads) / len(existing_spreads), 4) if existing_spreads else None,
            "average_candidate_short_long_iv_spread": round(sum(candidate_spreads) / len(candidate_spreads), 4) if candidate_spreads else None,
            "min_existing_short_long_iv_spread": min(existing_spreads) if existing_spreads else None,
            "max_existing_short_long_iv_spread": max(existing_spreads) if existing_spreads else None,
            "min_candidate_short_long_iv_spread": min(candidate_spreads) if candidate_spreads else None,
            "max_candidate_short_long_iv_spread": max(candidate_spreads) if candidate_spreads else None,
            "iv_spread_missing_slots": missing_iv_slots,
        }
    )
    return enriched


def build_passarelli_greeks_management(
    long_leg_analysis: Dict[str, Any],
    short_call_reviews: List[Dict[str, Any]],
    candidate_short_call: Dict[str, Any],
    diagonal_risk: Dict[str, Any],
) -> Dict[str, Any]:
    long_delta_value = safe_float(long_leg_analysis.get("net_long_delta"))
    if long_delta_value is None:
        long_delta_value = safe_float(long_leg_analysis.get("average_delta"))
    long_delta = long_delta_value or 0.0
    average_long_delta = safe_float(long_leg_analysis.get("average_delta"))
    time_weighted_delta = safe_float(long_leg_analysis.get("time_weighted_delta"))
    dte_risk_weighted_delta = safe_float(long_leg_analysis.get("dte_risk_weighted_delta"))
    min_long_dte = safe_int(long_leg_analysis.get("min_long_dte"))
    min_long_delta = safe_float(long_leg_analysis.get("min_long_delta"))
    max_long_gamma = safe_float(long_leg_analysis.get("max_long_gamma"))
    expiry_bucket_count = safe_int(long_leg_analysis.get("expiry_bucket_count")) or 0
    long_vega = safe_float(long_leg_analysis.get("net_long_vega")) or 0.0
    long_gamma = safe_float(long_leg_analysis.get("net_long_gamma")) or 0.0

    short_delta = 0.0
    short_gamma = 0.0
    short_theta = 0.0
    short_vega = 0.0
    max_short_delta = None
    min_short_dte = None
    triggers: List[str] = []
    recommendations: List[str] = []

    for item in short_call_reviews:
        quantity = max(safe_int(item.get("quantity")) or 0, 0)
        delta = safe_float(item.get("delta"))
        gamma = safe_float(item.get("gamma"))
        theta = safe_float(item.get("theta"))
        vega = safe_float(item.get("vega"))
        dte = safe_int(item.get("days_to_expiry"))

        if delta is not None:
            short_delta += delta * quantity
            max_short_delta = delta if max_short_delta is None else max(max_short_delta, delta)
        if gamma is not None:
            short_gamma += gamma * quantity
        if theta is not None:
            short_theta += theta * quantity
        if vega is not None:
            short_vega += vega * quantity
        if dte is not None:
            min_short_dte = dte if min_short_dte is None else min(min_short_dte, dte)

    candidate_delta = safe_float(candidate_short_call.get("delta"))
    candidate_gamma = safe_float(candidate_short_call.get("gamma"))
    candidate_dte = safe_int(candidate_short_call.get("days_to_expiry"))
    candidate_allowed = bool(candidate_short_call.get("consider_selling"))

    net_delta = long_delta - short_delta
    net_gamma = long_gamma - short_gamma
    net_theta = -short_theta
    net_vega = safe_float(diagonal_risk.get("net_vega_est"))
    if net_vega is None:
        net_vega = long_vega - short_vega

    if max_short_delta is not None and max_short_delta >= 0.65:
        triggers.append("short_delta_critical")
        recommendations.append("Roll now: short-call delta is at or above 0.65 and should be treated as a priority risk.")
    elif max_short_delta is not None and max_short_delta >= 0.50:
        triggers.append("short_delta_danger")
        recommendations.append("Roll up and out: short-call delta is at or above 0.50, so negative delta can quickly swallow long-leg upside.")
    elif max_short_delta is not None and max_short_delta >= 0.40:
        triggers.append("short_delta_warning")
        recommendations.append("Start planning a roll: short-call delta is at or above 0.40.")

    if min_short_dte is not None and min_short_dte <= 7:
        triggers.append("expiration_week_gamma")
        recommendations.append("Avoid holding the short call into expiration week; gamma risk can make net delta unstable.")
    elif min_short_dte is not None and min_short_dte <= 14:
        triggers.append("gamma_window")
        recommendations.append("Plan the short-call exit or roll inside the 1-2 week gamma-risk window.")

    if candidate_delta is not None and candidate_delta > 0.40:
        triggers.append("candidate_delta_too_high")
        recommendations.append("Do not open this candidate as-is: candidate delta is above the 0.25-0.40 PMCC target band.")

    if candidate_gamma is not None and candidate_dte is not None and candidate_dte <= 14 and candidate_gamma > 0.02:
        triggers.append("candidate_gamma_too_hot")
        recommendations.append("Avoid this near-expiry candidate: short gamma is high relative to the remaining time.")

    if expiry_bucket_count > 1:
        triggers.append("mixed_long_expiries")
        recommendations.append("Review LEAPS by expiry bucket: mixed long-leg expiries can make average delta understate the shortest-dated leg's gamma and DTE risk.")

    if min_long_dte is not None and min_long_dte < 270:
        triggers.append("shorter_dated_long_leg")
        recommendations.append("The shortest long call has less than 270 DTE; manage covered calls against that bucket more conservatively.")

    if min_long_delta is not None and min_long_delta < 0.80:
        triggers.append("weakest_long_delta_below_stock_substitute")
        recommendations.append("At least one long call has delta below 0.80; avoid using average delta alone to justify aggressive short calls.")

    if long_delta_value is None:
        triggers.append("long_delta_unavailable")
        recommendations.append("Long-leg delta is unavailable, so net delta cannot be evaluated from live Greeks.")
    elif net_delta <= 0:
        triggers.append("net_delta_non_positive")
        recommendations.append("Reduce or roll short-call exposure: PMCC should usually retain positive net delta when the thesis is bullish.")
    elif net_delta < 0.25:
        triggers.append("net_delta_low")
        recommendations.append("Keep short-call size or delta conservative; net delta is already low.")

    if net_vega > 1:
        triggers.append("net_long_vega")
        recommendations.append("The diagonal is net long vega; avoid adding exposure after a large IV run-up or before likely IV crush unless that is intentional.")

    if candidate_allowed and candidate_delta is not None and candidate_delta <= 0.40 and candidate_dte is not None and candidate_dte > 14:
        recommendations.append("Candidate is compatible with Passarelli-style Greeks control: short delta is moderate and not inside the worst gamma window.")

    if not recommendations:
        recommendations.append("No urgent Greeks adjustment trigger; monitor net delta, short gamma, and net vega before adding or rolling short calls.")

    return {
        "framework": "Passarelli Greeks exposure management",
        "net_delta_est": round(net_delta, 4),
        "net_gamma_est": round(net_gamma, 6),
        "net_theta_est": round(net_theta, 6),
        "net_vega_est": round(net_vega, 4),
        "long_delta_est": round(long_delta, 4),
        "average_long_delta": round(average_long_delta, 4) if average_long_delta is not None else None,
        "time_weighted_long_delta": round(time_weighted_delta, 4) if time_weighted_delta is not None else None,
        "dte_risk_weighted_long_delta": round(dte_risk_weighted_delta, 4) if dte_risk_weighted_delta is not None else None,
        "min_long_dte": min_long_dte,
        "min_long_delta": round(min_long_delta, 4) if min_long_delta is not None else None,
        "max_long_gamma": round(max_long_gamma, 6) if max_long_gamma is not None else None,
        "long_expiry_bucket_count": expiry_bucket_count,
        "short_delta_abs_est": round(short_delta, 4),
        "max_short_delta": round(max_short_delta, 4) if max_short_delta is not None else None,
        "min_short_dte": min_short_dte,
        "candidate_delta": candidate_delta,
        "candidate_gamma": candidate_gamma,
        "candidate_dte": candidate_dte,
        "triggers": triggers,
        "recommendations": recommendations,
        "rules": {
            "short_delta_warn": 0.40,
            "short_delta_danger": 0.50,
            "short_delta_critical": 0.65,
            "candidate_delta_target": [0.25, 0.40],
            "gamma_window_dte": 14,
            "expiration_week_dte": 7,
            "keep_bullish_net_delta_positive": True,
        },
    }


def analyze_short_call_position(
    short_call: PositionInput,
    stock_price: float,
    options: pd.DataFrame,
    config: StrategyConfig,
    iv_rank: Optional[float],
    base_positions: List[PositionInput],
    manual_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    option_row = find_option_row(options, short_call.raw_code)
    strike = short_call.strike
    expiry = short_call.expiry
    dte = None
    delta = None
    gamma = None
    vega = None
    theta = None
    iv = None
    profit_capture_pct = None
    mark = None

    if option_row is not None:
        strike = safe_float(option_row.get("strike_price")) or strike
        expiry = safe_text(option_row.get("strike_time")) or expiry
        dte = safe_int(option_row.get("days_to_expiry"))
        delta = safe_float(option_row.get("delta"))
        gamma = safe_float(option_row.get("gamma"))
        vega = safe_float(option_row.get("vega"))
        theta = safe_float(option_row.get("theta"))
        iv = safe_float(option_row.get("implied_volatility"))
        mark = option_mark_price(option_row.to_dict())
        credit = short_call.cost_price
        if credit is not None and credit > 0 and mark is not None:
            profit_capture_pct = (credit - mark) / credit * 100
    elif expiry is not None:
        expiry_date = parse_expiry(expiry)
        if expiry_date is not None:
            dte = (expiry_date - pd.Timestamp.today().date()).days

    if manual_metrics:
        delta = safe_float(manual_metrics.get("delta")) if safe_float(manual_metrics.get("delta")) is not None else delta
        iv = safe_float(manual_metrics.get("iv")) if safe_float(manual_metrics.get("iv")) is not None else iv
        profit_capture_pct = safe_float(manual_metrics.get("profit_capture_pct"))

    abs_delta = abs(delta) if delta is not None else None
    reasons: List[str] = []
    action = "MONITOR"
    rule_hits: List[str] = []
    stock_to_strike_pct = None

    if strike is not None:
        stock_to_strike_pct = (strike - stock_price) / stock_price * 100
        reasons.append(f"Stock is {stock_to_strike_pct:.2f}% away from short strike")

    if dte is not None:
        reasons.append(f"{dte} days to expiry")

    if delta is not None:
        reasons.append(f"Short-call delta {delta:.3f}")
    else:
        reasons.append("Short-call delta unavailable")

    if profit_capture_pct is not None:
        reasons.append(f"Profit captured {profit_capture_pct:.1f}%")

    if iv_rank is not None:
        reasons.append(f"IV rank {iv_rank:.1f}")

    if delta is not None and delta >= config.roll_delta_critical:
        rule_hits.append("critical_delta")
    elif delta is not None and delta >= config.roll_delta_danger:
        rule_hits.append("danger_delta")
    elif delta is not None and delta >= config.roll_delta_warn:
        rule_hits.append("warn_delta")

    if strike is not None and stock_price >= strike:
        rule_hits.append("itm")
    elif strike is not None and stock_price >= strike * 0.98:
        rule_hits.append("near_strike")
    elif strike is not None and stock_price >= strike * 0.90:
        rule_hits.append("warning_zone")

    if dte is not None and dte < config.roll_dte_urgent:
        rule_hits.append("urgent_dte")
    elif dte is not None and dte < config.roll_dte_active:
        rule_hits.append("active_dte")
    elif dte is not None and dte <= config.roll_dte_attention:
        rule_hits.append("attention_dte")

    if profit_capture_pct is not None and profit_capture_pct >= config.roll_profit_strong:
        rule_hits.append("strong_profit")
    elif profit_capture_pct is not None and profit_capture_pct >= config.roll_profit_take:
        rule_hits.append("profit_take")

    if iv_rank is not None and iv_rank >= config.high_iv_roll_threshold:
        rule_hits.append("high_iv")

    min_leaps_strike = min((item.strike for item in base_positions if item.strike is not None), default=None)
    leaps_protection = False
    if min_leaps_strike is not None and strike is not None and stock_price >= strike:
        intrinsic_buffer = strike - min_leaps_strike
        if intrinsic_buffer / stock_price < 0.45:
            leaps_protection = True
            rule_hits.append("leaps_protection")

    if "critical_delta" in rule_hits or ("itm" in rule_hits and "urgent_dte" in rule_hits):
        action = "ROLL_NOW"
        reasons.append("Critical roll condition hit")
    elif "danger_delta" in rule_hits or ("near_strike" in rule_hits and "active_dte" in rule_hits):
        action = "ROLL_UP_OUT"
        reasons.append("Danger zone: delta or price proximity is elevated")
    elif "strong_profit" in rule_hits and ("active_dte" in rule_hits or "warning_zone" in rule_hits or "high_iv" in rule_hits):
        action = "TAKE_PROFIT_AND_RESELL"
        if "active_dte" in rule_hits:
            reasons.append("Most of the credit has been captured while the roll window is active")
        else:
            reasons.append("Most of the credit has been captured and IV/risk conditions support recycling the short call")
    elif "profit_take" in rule_hits and ("active_dte" in rule_hits or "high_iv" in rule_hits):
        action = "PREPARE_ROLL"
        if "active_dte" in rule_hits:
            reasons.append("Profit target reached inside the normal roll window")
        else:
            reasons.append("Profit target reached and high IV supports considering an earlier roll")
    elif "warn_delta" in rule_hits or "active_dte" in rule_hits or "near_strike" in rule_hits:
        action = "PLAN_ROLL"
        reasons.append("Roll window is active, start planning the next cycle")
    elif "attention_dte" in rule_hits:
        action = "REVIEW_EXPIRY"
        reasons.append("Expiry is inside the 14-21 DTE attention zone; review the roll plan")
    elif strike is not None and stock_price <= strike * 0.90:
        action = "HOLD_DECAY"
        reasons.append("Short call is still comfortably OTM")

    if "high_iv" in rule_hits and action in {"PLAN_ROLL", "PREPARE_ROLL", "ROLL_UP_OUT", "ROLL_NOW"}:
        reasons.append("High IV supports rolling up and out more aggressively")
    if leaps_protection:
        reasons.append("Protect LEAPS intrinsic exposure; do not let the short call stay deep ITM")

    return ShortCallReview(
        code=short_call.raw_code,
        quantity=short_call.quantity,
        strike=strike,
        expiry=expiry,
        days_to_expiry=dte,
        delta=delta,
        abs_delta=abs_delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        iv=iv,
        profit_capture_pct=profit_capture_pct,
        price_to_strike_pct=stock_to_strike_pct,
        iv_rank=iv_rank,
        roll_action=action,
        rule_hits=rule_hits,
        reason=reasons,
    ).to_dict() | {
        "cost_price": short_call.cost_price,
        "mark_price": round(mark, 4) if mark is not None else None,
    }


def option_mark_price(option: Dict[str, Any]) -> Optional[float]:
    bid = safe_float(option.get("bid_price"))
    ask = safe_float(option.get("ask_price"))
    last = safe_float(option.get("last_price"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2
    if last is not None and last > 0:
        return last
    if bid is not None and bid > 0:
        return bid
    if ask is not None and ask > 0:
        return ask
    return None


def build_roll_candidate_record(
    row: pd.Series,
    short_review: Dict[str, Any],
    stock_price: float,
    old_buyback_price: Optional[float],
    score: float,
) -> Dict[str, Any]:
    candidate = row.to_dict()
    strike = safe_float(candidate.get("strike_price"))
    dte = safe_int(candidate.get("days_to_expiry"))
    bid = safe_float(candidate.get("bid_price"))
    ask = safe_float(candidate.get("ask_price"))
    mark = option_mark_price(candidate)
    estimated_net_credit = None
    if bid is not None and old_buyback_price is not None:
        estimated_net_credit = bid - old_buyback_price

    return {
        "code": candidate.get("code"),
        "roll_from": short_review.get("code"),
        "strike": strike,
        "expiry": candidate.get("strike_time"),
        "days_to_expiry": dte,
        "delta": safe_float(candidate.get("delta")),
        "gamma": safe_float(candidate.get("gamma")),
        "vega": safe_float(candidate.get("vega")),
        "theta": safe_float(candidate.get("theta")),
        "iv": safe_float(candidate.get("implied_volatility")),
        "bid_price": bid,
        "ask_price": ask,
        "last_price": safe_float(candidate.get("last_price")),
        "mark_price": round(mark, 3) if mark is not None else None,
        "volume": safe_int(candidate.get("volume")),
        "open_interest": safe_int(candidate.get("open_interest")),
        "otm_percent": round((strike - stock_price) / stock_price * 100, 2) if strike is not None else None,
        "strike_lift": round(strike - short_review.get("strike"), 2)
        if strike is not None and safe_float(short_review.get("strike")) is not None
        else None,
        "dte_extension": dte - short_review.get("days_to_expiry")
        if dte is not None and safe_int(short_review.get("days_to_expiry")) is not None
        else None,
        "estimated_buyback_price": old_buyback_price,
        "estimated_net_credit": round(estimated_net_credit, 3) if estimated_net_credit is not None else None,
        "estimated_net_debit": round(-estimated_net_credit, 3)
        if estimated_net_credit is not None and estimated_net_credit < 0
        else None,
        "selection_score": round(score, 2),
    }


def find_roll_candidates_for_short_call(
    short_review: Dict[str, Any],
    options: pd.DataFrame,
    stock_price: float,
    config: StrategyConfig,
    long_leg_analysis: Optional[Dict[str, Any]] = None,
    portfolio_identity: Optional[Dict[str, Any]] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if options.empty:
        return []

    old_strike = safe_float(short_review.get("strike"))
    old_dte = safe_int(short_review.get("days_to_expiry"))
    if old_strike is None:
        return []

    old_row = find_option_row(options, str(short_review.get("code")))
    old_buyback_price = None
    if old_row is not None:
        old_buyback_price = safe_float(old_row.get("ask_price")) or option_mark_price(old_row.to_dict())

    working = options.copy()
    for column in [
        "strike_price",
        "days_to_expiry",
        "delta",
        "gamma",
        "vega",
        "theta",
        "implied_volatility",
        "bid_price",
        "ask_price",
        "last_price",
        "volume",
        "open_interest",
    ]:
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")

    min_dte = max(config.preferred_dte_min, old_dte or config.preferred_dte_min)
    max_dte = max(config.preferred_dte_max, min_dte + 21)
    delta_low = max(0.15, config.target_delta_low - 0.07)
    delta_high = config.target_delta_high

    candidates = working[
        (working["strike_price"] > old_strike)
        & pd.to_numeric(working["days_to_expiry"], errors="coerce").between(min_dte, max_dte, inclusive="both")
        & pd.to_numeric(working["delta"], errors="coerce").between(delta_low, delta_high, inclusive="both")
    ].copy()

    if candidates.empty:
        candidates = working[
            (working["strike_price"] > old_strike)
            & pd.to_numeric(working["days_to_expiry"], errors="coerce").between(min_dte, max_dte, inclusive="both")
            & (pd.to_numeric(working["delta"], errors="coerce") <= config.target_delta_high)
        ].copy()

    if candidates.empty:
        return []

    target_dte = max((old_dte or config.preferred_dte_min) + 10, 32)
    target_delta = (config.target_delta_low + config.target_delta_high) / 2 - 0.025
    candidates["roll_score"] = 0.0
    candidates["roll_score"] += 20 - (candidates["days_to_expiry"] - target_dte).abs() * 0.45
    candidates["roll_score"] += 24 - (candidates["delta"] - target_delta).abs() * 85
    candidates["roll_score"] += (candidates["strike_price"] - old_strike).clip(upper=30) * 0.18
    if "open_interest" in candidates.columns:
        candidates["roll_score"] += candidates["open_interest"].fillna(0).clip(upper=1000) / 250
    if "volume" in candidates.columns:
        candidates["roll_score"] += candidates["volume"].fillna(0).clip(upper=1000) / 500
    if old_buyback_price is not None and "bid_price" in candidates.columns:
        net_credit = candidates["bid_price"] - old_buyback_price
        candidates["roll_score"] += net_credit.clip(lower=-8, upper=2) * 0.8

    sorted_candidates = candidates.sort_values(
        by=["roll_score", "days_to_expiry", "strike_price"],
        ascending=[False, True, True],
    ).head(limit)

    records = [
        build_roll_candidate_record(row, short_review, stock_price, old_buyback_price, safe_float(row.get("roll_score")) or 0.0)
        for _, row in sorted_candidates.iterrows()
    ]
    if long_leg_analysis:
        for record in records:
            record["whole_symbol_roll_pnl"] = estimate_whole_symbol_roll_pnl(
                short_review,
                record,
                long_leg_analysis,
                stock_price=stock_price,
                portfolio_identity=portfolio_identity,
            )
    return records


def get_option_chain_for_type(symbol: str, ctx: OpenQuoteContext, option_type: OptionType, expiries: List[str]) -> pd.DataFrame:
    chains: List[pd.DataFrame] = []
    errors: List[str] = []
    for expiry in sorted({item for item in expiries if item}):
        kwargs = {"code": symbol, "start": expiry, "end": expiry, "option_type": option_type}
        try:
            throttle_option_chain_request()
            ret, data = ctx.get_option_chain(**kwargs)
        except TypeError as exc:
            errors.append(f"{kwargs}: TypeError({exc})")
            continue
        if ret == RET_OK and data is not None and not data.empty:
            chains.append(normalize_option_chain(data))
        else:
            errors.append(f"{kwargs}: {format_futu_error(ret, data)}")

    if chains:
        return normalize_option_chain(pd.concat(chains, ignore_index=True).drop_duplicates(subset=["code"]))
    return pd.DataFrame()


def enrich_put_options(chain: pd.DataFrame, greeks: pd.DataFrame, stock_price: float) -> pd.DataFrame:
    enriched = enrich_options(chain, greeks, stock_price)
    if enriched.empty:
        return enriched
    enriched["put_otm_pct"] = (stock_price - pd.to_numeric(enriched["strike_price"], errors="coerce")) / stock_price
    enriched["is_otm"] = pd.to_numeric(enriched["strike_price"], errors="coerce") <= stock_price
    return enriched


def build_wheel_state_for_short_put(
    stock_price: float,
    strike: Optional[float],
    dte: Optional[int],
    delta: Optional[float],
    profit_capture_pct: Optional[float],
    break_even: Optional[float],
    mark: Optional[float],
    credit: Optional[float],
) -> Dict[str, Any]:
    abs_delta = abs(delta) if delta is not None else None
    otm_pct = (stock_price - strike) / stock_price * 100 if strike is not None and stock_price else None
    assignment_acceptable = break_even is not None and stock_price >= break_even
    state = "CSP_OPEN"
    action = "HOLD"
    priority = "LOW"
    next_check_trigger = "Review again if spot falls near the short strike, delta rises, or DTE reaches 21."
    rationale: List[str] = []

    if profit_capture_pct is not None and profit_capture_pct >= 70:
        state = "CSP_PROFIT_TAKE_STRONG"
        action = "BUY_TO_CLOSE_OR_ROLL_NEW_CYCLE"
        priority = "MEDIUM"
        next_check_trigger = "Buy back if the ask is near the 70% capture target, or recycle into a new put only if IV/price are attractive."
        rationale.append("At least 70% of the original credit has been captured.")
    elif profit_capture_pct is not None and profit_capture_pct >= 50:
        state = "CSP_PROFIT_TAKE"
        action = "CONSIDER_BUY_TO_CLOSE"
        priority = "MEDIUM"
        next_check_trigger = "Buy back near the 50% capture target to release buying power."
        rationale.append("At least 50% of the original credit has been captured.")

    defensive = False
    if strike is not None and stock_price <= strike:
        defensive = True
        state = "CSP_DEFEND_OR_ACCEPT_ASSIGNMENT"
        action = "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT"
        priority = "HIGH"
        next_check_trigger = "Decide now whether you want shares at this strike; otherwise roll down/out before the put moves deeper ITM."
        rationale.append("Spot is at or below the short put strike.")
    elif abs_delta is not None and abs_delta >= 0.50:
        defensive = True
        state = "CSP_DEFEND_OR_ACCEPT_ASSIGNMENT"
        action = "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT"
        priority = "HIGH"
        next_check_trigger = "Short-put delta is high; compare assignment versus rolling down/out."
        rationale.append("Short-put absolute delta is at or above 0.50.")
    elif (abs_delta is not None and abs_delta >= 0.30) or (otm_pct is not None and otm_pct <= 5):
        defensive = True
        state = "CSP_DEFENSE_PREP"
        action = "PREPARE_ROLL_DOWN_OUT"
        priority = "MEDIUM"
        next_check_trigger = "Prepare a roll if spot keeps falling or delta approaches 0.50."
        rationale.append("Short put is approaching the defensive zone.")

    if not defensive and dte is not None and dte <= 21 and state == "CSP_OPEN":
        state = "CSP_MANAGEMENT_WINDOW"
        action = "REVIEW_HOLD_OR_CLOSE"
        priority = "MEDIUM"
        next_check_trigger = "Inside 21 DTE, review daily; close if profit improves or defend if spot weakens."
        rationale.append("Short put is inside the normal 21 DTE management window.")

    if mark is not None and credit is not None:
        rationale.append(f"Current mark is {mark:.2f} versus original credit {credit:.2f}.")
    if otm_pct is not None:
        rationale.append(f"Underlying is {otm_pct:.2f}% above the strike.")
    if break_even is not None:
        rationale.append(f"Cash-secured break-even is {break_even:.2f}.")

    return {
        "state": state,
        "action": action,
        "priority": priority,
        "assignment_acceptable_by_breakeven": assignment_acceptable,
        "next_check_trigger": next_check_trigger,
        "rationale": rationale,
    }


def build_short_put_payoff_scenarios(
    short_put: PositionInput,
    stock_price: float,
    strike: Optional[float],
    credit: Optional[float],
    mark: Optional[float],
) -> Dict[str, Any]:
    if strike is None or credit is None or stock_price <= 0:
        return {
            "style": "short_put_expiration_payoff",
            "rows": [],
            "summary": "Missing strike, credit, or underlying price; payoff scenarios unavailable.",
            "operation_advice": "MONITOR_DATA",
        }

    quantity = max(short_put.quantity, 1)
    multiplier = 100
    break_even = strike - credit
    current_unrealized = None
    if mark is not None:
        current_unrealized = (credit - mark) * multiplier * quantity

    raw_spots = [
        stock_price * 0.70,
        stock_price * 0.80,
        stock_price * 0.85,
        stock_price * 0.90,
        stock_price * 0.95,
        break_even,
        strike,
        stock_price,
        stock_price * 1.05,
        stock_price * 1.10,
        stock_price * 1.20,
    ]
    scenario_spots = sorted({round(item, 2) for item in raw_spots if item and item > 0})
    rows: List[Dict[str, Any]] = []
    for scenario_spot in scenario_spots:
        intrinsic = max(strike - scenario_spot, 0)
        pnl_per_share = credit - intrinsic
        pnl_total = pnl_per_share * multiplier * quantity
        assigned = scenario_spot < strike
        rows.append(
            {
                "spot_at_expiry": round(scenario_spot, 2),
                "spot_change_pct": round((scenario_spot / stock_price - 1) * 100, 2),
                "put_intrinsic_at_expiry": round(intrinsic, 2),
                "pnl_per_share": round(pnl_per_share, 2),
                "pnl_total": round(pnl_total, 2),
                "assigned": assigned,
                "assignment_cash_required": round(strike * multiplier * quantity, 2) if assigned else 0.0,
                "effective_share_cost": round(break_even, 2) if assigned else None,
            }
        )

    five_down = stock_price * 0.95
    ten_down = stock_price * 0.90
    twenty_down = stock_price * 0.80
    operation_advice = "HOLD"
    summary_parts: List[str] = [
        f"Expiration breakeven is {break_even:.2f}; max profit is {credit * multiplier * quantity:.2f}.",
    ]
    if current_unrealized is not None:
        summary_parts.append(f"Current estimated open P/L is {current_unrealized:.2f}.")

    if stock_price <= strike:
        operation_advice = "DEFEND_OR_ACCEPT_ASSIGNMENT"
        summary_parts.append("Spot is already below the short put strike; decide whether assignment is acceptable.")
    elif five_down <= strike:
        operation_advice = "PREPARE_DEFENSE"
        summary_parts.append("A 5% decline would put the short put near or in the money.")
    elif ten_down <= break_even:
        operation_advice = "WATCH_10PCT_DROP"
        summary_parts.append("A 10% decline would reach or pass the cash-secured breakeven.")
    elif twenty_down <= break_even:
        operation_advice = "HOLD_WITH_20PCT_STRESS_AWARENESS"
        summary_parts.append("Normal pullbacks still leave room, but a 20% stress move reaches the loss zone.")
    else:
        summary_parts.append("The current buffer is wide under the standard stress points.")

    return {
        "style": "short_put_expiration_payoff",
        "multiplier": multiplier,
        "quantity": quantity,
        "underlying_price": round(stock_price, 2),
        "strike": round(strike, 2),
        "credit": round(credit, 2),
        "mark": round(mark, 3) if mark is not None else None,
        "break_even": round(break_even, 2),
        "max_profit": round(credit * multiplier * quantity, 2),
        "max_loss_cash_secured": round(break_even * multiplier * quantity, 2),
        "current_unrealized_pnl": round(current_unrealized, 2) if current_unrealized is not None else None,
        "rows": rows,
        "summary": " ".join(summary_parts),
        "operation_advice": operation_advice,
    }


def analyze_short_put_position(
    short_put: PositionInput,
    stock_price: float,
    put_options: pd.DataFrame,
    iv_rank: Optional[float],
) -> Dict[str, Any]:
    option_row = find_option_row(put_options, short_put.raw_code)
    strike = short_put.strike
    expiry = short_put.expiry
    dte = None
    delta = None
    gamma = None
    vega = None
    theta = None
    iv = None
    mark = None
    bid = None
    ask = None
    last = None

    if option_row is not None:
        strike = safe_float(option_row.get("strike_price")) or strike
        expiry = safe_text(option_row.get("strike_time")) or expiry
        dte = safe_int(option_row.get("days_to_expiry"))
        delta = safe_float(option_row.get("delta"))
        gamma = safe_float(option_row.get("gamma"))
        vega = safe_float(option_row.get("vega"))
        theta = safe_float(option_row.get("theta"))
        iv = safe_float(option_row.get("implied_volatility"))
        bid = safe_float(option_row.get("bid_price"))
        ask = safe_float(option_row.get("ask_price"))
        last = safe_float(option_row.get("last_price"))
        mark = option_mark_price(option_row.to_dict())
    elif expiry is not None:
        expiry_date = parse_expiry(expiry)
        if expiry_date is not None:
            dte = (expiry_date - pd.Timestamp.today().date()).days

    credit = short_put.cost_price
    profit_capture_pct = None
    if credit is not None and mark is not None and credit > 0:
        profit_capture_pct = (credit - mark) / credit * 100

    break_even = None
    max_loss = None
    if strike is not None and credit is not None:
        break_even = strike - credit
        max_loss = break_even * 100 * max(short_put.quantity, 1)

    otm_pct = None
    if strike is not None and stock_price:
        otm_pct = (stock_price - strike) / stock_price * 100

    action = "MONITOR"
    rule_hits: List[str] = []
    reasons: List[str] = []

    if otm_pct is not None:
        reasons.append(f"Underlying is {otm_pct:.2f}% above short put strike")
    if dte is not None:
        reasons.append(f"{dte} days to expiry")
    if delta is not None:
        reasons.append(f"Short-put contract Delta {delta:.3f}; |Delta| {abs(delta):.3f}")
    else:
        reasons.append("Short-put delta unavailable")
    if profit_capture_pct is not None:
        reasons.append(f"Profit captured {profit_capture_pct:.1f}%")
    if iv_rank is not None:
        reasons.append(f"IV rank {iv_rank:.1f}")

    abs_delta = abs(delta) if delta is not None else None
    if profit_capture_pct is not None and profit_capture_pct >= 70:
        rule_hits.append("strong_profit")
    elif profit_capture_pct is not None and profit_capture_pct >= 50:
        rule_hits.append("profit_take")

    if abs_delta is not None and abs_delta >= 0.50:
        rule_hits.append("put_delta_danger")
    elif abs_delta is not None and abs_delta >= 0.30:
        rule_hits.append("put_delta_warning")
    elif abs_delta is not None and abs_delta >= 0.20:
        rule_hits.append("put_delta_attention")

    if strike is not None:
        if stock_price <= strike:
            rule_hits.append("itm")
        elif stock_price <= strike * 1.05:
            rule_hits.append("near_strike")
        elif stock_price <= strike * 1.10:
            rule_hits.append("attention_zone")

    if dte is not None and dte <= 7:
        rule_hits.append("expiration_week")
    elif dte is not None and dte <= 21:
        rule_hits.append("management_window")

    if "itm" in rule_hits or "put_delta_danger" in rule_hits:
        action = "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT"
        reasons.append("Short put is in the defensive zone; decide between assignment and rolling down/out.")
    elif "near_strike" in rule_hits or "put_delta_warning" in rule_hits:
        action = "PREPARE_DEFENSE"
        reasons.append("Prepare to roll down/out if the selloff continues.")
    elif "profit_take" in rule_hits:
        action = "TAKE_PROFIT"
        reasons.append("Profit target reached; consider buying back to release cash/risk.")
    elif "management_window" in rule_hits or "put_delta_attention" in rule_hits or "attention_zone" in rule_hits:
        action = "REVIEW"
        reasons.append("Review the short put as DTE, delta, or spot distance is moving into the attention zone.")

    wheel_state = build_wheel_state_for_short_put(
        stock_price=stock_price,
        strike=strike,
        dte=dte,
        delta=delta,
        profit_capture_pct=profit_capture_pct,
        break_even=break_even,
        mark=mark,
        credit=credit,
    )
    payoff_scenarios = build_short_put_payoff_scenarios(
        short_put=short_put,
        stock_price=stock_price,
        strike=strike,
        credit=credit,
        mark=mark,
    )
    operation_advice_text = build_short_put_operation_advice(
        {
            "code": short_put.raw_code,
            "roll_action": action,
            "delta": delta,
            "abs_delta": round(abs_delta, 4) if abs_delta is not None else None,
            "days_to_expiry": dte,
            "underlying_to_strike_pct": round(otm_pct, 2) if otm_pct is not None else None,
            "close_target_50pct": round(credit * 0.5, 2) if credit is not None else None,
            "wheel_state": wheel_state,
            "payoff_scenarios": payoff_scenarios,
        }
    )

    return ShortPutReview(
        code=short_put.raw_code,
        strategy_type="short_put",
        quantity=short_put.quantity,
        strike=strike,
        expiry=expiry,
        days_to_expiry=dte,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        iv=iv,
        bid_price=bid,
        ask_price=ask,
        last_price=last,
        mark_price=mark,
        credit_received=credit,
        profit_capture_pct=profit_capture_pct,
        break_even=break_even,
        estimated_assignment_cost=round(strike * 100 * short_put.quantity, 2) if strike is not None else None,
        max_loss_if_cash_secured=max_loss,
        close_target_50pct=round(credit * 0.5, 2) if credit is not None else None,
        close_target_70pct=round(credit * 0.3, 2) if credit is not None else None,
        underlying_to_strike_pct=otm_pct,
        iv_rank=iv_rank,
        roll_action=action,
        wheel_state=wheel_state,
        payoff_scenarios=payoff_scenarios,
        operation_advice_text=operation_advice_text,
        rule_hits=rule_hits,
        reason=reasons,
    ).to_dict()


def build_put_roll_candidate_record(
    row: pd.Series,
    put_review: Dict[str, Any],
    stock_price: float,
    old_buyback_price: Optional[float],
    score: float,
) -> Dict[str, Any]:
    candidate = row.to_dict()
    strike = safe_float(candidate.get("strike_price"))
    dte = safe_int(candidate.get("days_to_expiry"))
    bid = safe_float(candidate.get("bid_price"))
    ask = safe_float(candidate.get("ask_price"))
    mark = option_mark_price(candidate)
    estimated_net_credit = None
    if bid is not None and old_buyback_price is not None:
        estimated_net_credit = bid - old_buyback_price

    old_strike = safe_float(put_review.get("strike"))
    return {
        "code": candidate.get("code"),
        "roll_from": put_review.get("code"),
        "strike": strike,
        "expiry": candidate.get("strike_time"),
        "days_to_expiry": dte,
        "delta": safe_float(candidate.get("delta")),
        "gamma": safe_float(candidate.get("gamma")),
        "vega": safe_float(candidate.get("vega")),
        "theta": safe_float(candidate.get("theta")),
        "iv": safe_float(candidate.get("implied_volatility")),
        "bid_price": bid,
        "ask_price": ask,
        "last_price": safe_float(candidate.get("last_price")),
        "mark_price": round(mark, 3) if mark is not None else None,
        "volume": safe_int(candidate.get("volume")),
        "open_interest": safe_int(candidate.get("open_interest")),
        "otm_percent": round((stock_price - strike) / stock_price * 100, 2) if strike is not None else None,
        "strike_change": round(strike - old_strike, 2) if strike is not None and old_strike is not None else None,
        "dte_extension": dte - put_review.get("days_to_expiry")
        if dte is not None and safe_int(put_review.get("days_to_expiry")) is not None
        else None,
        "estimated_buyback_price": old_buyback_price,
        "estimated_net_credit": round(estimated_net_credit, 3) if estimated_net_credit is not None else None,
        "estimated_net_debit": round(-estimated_net_credit, 3)
        if estimated_net_credit is not None and estimated_net_credit < 0
        else None,
        "selection_score": round(score, 2),
    }


def find_roll_candidates_for_short_put(
    put_review: Dict[str, Any],
    put_options: pd.DataFrame,
    stock_price: float,
    config: StrategyConfig,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if put_options.empty:
        return []

    old_strike = safe_float(put_review.get("strike"))
    old_dte = safe_int(put_review.get("days_to_expiry"))
    if old_strike is None:
        return []

    old_row = find_option_row(put_options, str(put_review.get("code")))
    old_buyback_price = None
    if old_row is not None:
        old_buyback_price = safe_float(old_row.get("ask_price")) or option_mark_price(old_row.to_dict())

    working = put_options.copy()
    for column in [
        "strike_price",
        "days_to_expiry",
        "delta",
        "gamma",
        "vega",
        "theta",
        "implied_volatility",
        "bid_price",
        "ask_price",
        "last_price",
        "volume",
        "open_interest",
    ]:
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")

    min_dte = max(config.preferred_dte_min, old_dte or config.preferred_dte_min)
    max_dte = max(config.preferred_dte_max, min_dte + 21)
    delta_abs = pd.to_numeric(working["delta"], errors="coerce").abs()
    candidates = working[
        (working["strike_price"] < old_strike)
        & pd.to_numeric(working["days_to_expiry"], errors="coerce").between(min_dte, max_dte, inclusive="both")
        & delta_abs.between(0.08, 0.30, inclusive="both")
    ].copy()

    if candidates.empty:
        candidates = working[
            (working["strike_price"] < old_strike)
            & pd.to_numeric(working["days_to_expiry"], errors="coerce").between(min_dte, max_dte, inclusive="both")
            & (delta_abs <= 0.35)
        ].copy()

    if candidates.empty:
        return []

    target_dte = max((old_dte or config.preferred_dte_min) + 10, 32)
    target_delta_abs = 0.20
    candidates["delta_abs"] = pd.to_numeric(candidates["delta"], errors="coerce").abs()
    candidates["roll_score"] = 0.0
    candidates["roll_score"] += 20 - (candidates["days_to_expiry"] - target_dte).abs() * 0.45
    candidates["roll_score"] += 24 - (candidates["delta_abs"] - target_delta_abs).abs() * 85
    candidates["roll_score"] += (old_strike - candidates["strike_price"]).clip(upper=30) * 0.20
    if "open_interest" in candidates.columns:
        candidates["roll_score"] += candidates["open_interest"].fillna(0).clip(upper=1000) / 250
    if "volume" in candidates.columns:
        candidates["roll_score"] += candidates["volume"].fillna(0).clip(upper=1000) / 500
    if old_buyback_price is not None and "bid_price" in candidates.columns:
        net_credit = candidates["bid_price"] - old_buyback_price
        candidates["roll_score"] += net_credit.clip(lower=-8, upper=2) * 0.8

    sorted_candidates = candidates.sort_values(
        by=["roll_score", "days_to_expiry", "strike_price"],
        ascending=[False, True, False],
    ).head(limit)

    return [
        build_put_roll_candidate_record(row, put_review, stock_price, old_buyback_price, safe_float(row.get("roll_score")) or 0.0)
        for _, row in sorted_candidates.iterrows()
    ]


def load_call_market_data(symbol: str, ctx: OpenQuoteContext, config: StrategyConfig) -> Dict[str, Any]:
    quote, quote_source = get_quote(symbol, ctx)
    stock_price = safe_float(quote.get("last_price"))
    if stock_price is None:
        raise RuntimeError(f"Quote for {symbol} did not include a usable last price.")

    chain = get_option_chain(symbol, ctx, config)
    greeks = get_greeks(chain["code"].tolist(), ctx)
    enriched = enrich_options(chain, greeks, stock_price)
    data_quality = require_option_data_quality(symbol, enriched)
    put_chain = pd.DataFrame()
    put_greeks = pd.DataFrame()
    put_enriched = pd.DataFrame()
    put_error = None
    try:
        preferred_dates = get_preferred_expiry_dates(symbol, ctx, config)
        put_chain = get_option_chain_for_type(symbol, ctx, OptionType.PUT, preferred_dates)
        if not put_chain.empty:
            put_greeks = get_greeks(put_chain["code"].tolist(), ctx)
            put_enriched = enrich_put_options(put_chain, put_greeks, stock_price)
    except Exception as exc:
        put_error = enrich_error_message(str(exc))

    iv_snapshot = estimate_pmcc_iv_snapshot(enriched, stock_price, put_enriched)
    try:
        iv_snapshot["history_record"] = record_pmcc_iv_history(symbol, iv_snapshot)
    except Exception as exc:
        iv_snapshot["history_record"] = {
            "status": "ERROR",
            "error": enrich_error_message(str(exc)),
            "path": str(IV_HISTORY_FILE),
        }
    if put_error:
        iv_snapshot["put_side_warning"] = put_error

    return {
        "quote": quote,
        "quote_source": quote_source,
        "stock_price": stock_price,
        "chain": chain,
        "greeks": greeks,
        "enriched": enriched,
        "put_chain": put_chain,
        "put_greeks": put_greeks,
        "put_enriched": put_enriched,
        "put_error": put_error,
        "iv_snapshot": iv_snapshot,
        "data_quality": data_quality,
    }


def run_futu_opend_preflight(symbol: str, config: StrategyConfig) -> Dict[str, Any]:
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        quote, quote_source = get_quote(symbol, ctx)
        stock_price = safe_float(quote.get("last_price"))
        if stock_price is None:
            raise RuntimeError(f"Startup check got a quote for {symbol}, but last_price was missing.")

        expiries = get_option_expiries(symbol, ctx)
        if expiries.empty:
            raise RuntimeError(f"Startup check could not read option expiries for {symbol}.")

        chain = get_option_chain(symbol, ctx, config)
        if chain.empty:
            raise RuntimeError(f"Startup check could not read option chain for {symbol}.")

        sample_codes = chain["code"].dropna().astype(str).head(80).tolist()
        greeks = get_greeks(sample_codes, ctx)
        enriched = enrich_options(chain[chain["code"].isin(sample_codes)].copy(), greeks, stock_price)
        data_quality = require_option_data_quality(symbol, enriched)

        return {
            "status": "OK",
            "symbol": symbol,
            "quote_source": quote_source,
            "last_price": round(stock_price, 4),
            "expiry_count": int(len(expiries)),
            "chain_contracts_sampled": int(len(sample_codes)),
            "data_quality": data_quality,
        }
    except Exception as exc:
        raise RuntimeError(
            enrich_error_message(
                f"Futu OpenD startup check failed for {symbol}: {exc}. "
                "Please confirm OpenD is running and logged in, US market quotes are enabled, "
                "and the connected account can read US option chains and Greeks."
            )
        ) from exc
    finally:
        ctx.close()


def decision_engine(
    symbol: str,
    config: StrategyConfig = DEFAULT_CONFIG,
    ctx: Optional[OpenQuoteContext] = None,
    call_market_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    owns_ctx = ctx is None
    if ctx is None:
        ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        if call_market_data is None:
            call_market_data = load_call_market_data(symbol, ctx, config)
        quote_source = call_market_data["quote_source"]
        quote = call_market_data["quote"]
        stock_price = call_market_data["stock_price"]
        enriched = call_market_data["enriched"].copy()
        put_enriched_for_iv = call_market_data.get("put_enriched")
        iv_snapshot = call_market_data.get("iv_snapshot") or estimate_pmcc_iv_snapshot(
            enriched,
            stock_price,
            put_enriched_for_iv if isinstance(put_enriched_for_iv, pd.DataFrame) else pd.DataFrame(),
        )
        data_quality = call_market_data.get("data_quality") or build_option_data_quality(symbol, enriched)
        underlying_iv = safe_float(iv_snapshot.get("iv")) or estimate_underlying_iv(enriched, stock_price)
        if isinstance(put_enriched_for_iv, pd.DataFrame) and not put_enriched_for_iv.empty:
            iv_proxy_options = pd.concat([enriched, put_enriched_for_iv], ignore_index=True)
        else:
            iv_proxy_options = enriched
        chain_iv_rank_proxy_meta = estimate_chain_iv_rank_proxy(iv_proxy_options, stock_price)
        chain_iv_rank_proxy = safe_float(chain_iv_rank_proxy_meta.get("value"))
        iv_rank_analysis = build_iv_rank_analysis(symbol, underlying_iv, chain_iv_rank_proxy, chain_iv_rank_proxy_meta, config)
        iv_rank = safe_float(iv_rank_analysis.get("iv_rank"))
        iv_percentile = safe_float(iv_rank_analysis.get("iv_percentile"))
        hv = estimate_historical_volatility(symbol, ctx)
        trend = get_trend(symbol, ctx, config.history_bars)
        enriched, trend, iv_rank, iv_percentile, underlying_iv, hv, manual_inputs = apply_manual_overrides(
            enriched,
            trend,
            iv_rank,
            iv_percentile,
            underlying_iv,
            hv,
            INTERACTIVE_MODE,
            config,
        )
        if "iv_rank" in manual_inputs:
            remember_iv_rank_value(
                symbol,
                iv_rank,
                manual_inputs.get("iv_rank_source", "manual_override"),
                iv_percentile,
            )
            iv_rank_analysis = {
                **iv_rank_analysis,
                "iv_rank": iv_rank,
                "iv_percentile": iv_percentile,
                "source": manual_inputs.get("iv_rank_source", "manual_override"),
                "method": "user supplied historical IV Rank",
                "is_true_historical_iv_rank": True,
                "is_decision_usable_iv_rank": True,
                "priority": "interactive_manual_override",
            }
        data_validation = build_market_data_validation(symbol, stock_price, config.enable_web_validation)
        event_block = build_event_risk_block(symbol, quote, data_validation, config)

        selected = select_option(enriched, config)
        iv_environment = build_iv_environment(underlying_iv, hv, iv_rank, iv_percentile, selected)
        action_block = build_action(iv_rank, trend, stock_price, selected, config, iv_environment)
        if event_block.get("blocked"):
            action_block["action"] = "WAIT"
            action_block.setdefault("reason", []).append(
                "Near-term event block is active: " + summarize_event_block(event_block)
            )
        roll_action = None
        if config.short_call_strike is not None:
            if stock_price >= config.short_call_strike * 0.98:
                roll_action = "ROLL_UP"
            elif stock_price <= config.short_call_strike * 0.90:
                roll_action = "HOLD_DECAY"
            else:
                roll_action = "MONITOR"

        return {
            "symbol": symbol,
            "price": round(stock_price, 2),
            "quote_source": quote_source,
            "data_quality": data_quality,
            "data_validation": data_validation,
            "event_block": event_block,
            "trend": trend,
            "iv_rank_est": iv_rank,
            "iv_percentile": iv_percentile,
            "iv_rank_analysis": iv_rank_analysis,
            "iv": underlying_iv,
            "hv": hv,
            "iv_snapshot": iv_snapshot,
            "iv_environment": iv_environment,
            "action": action_block["action"],
            "reason": action_block["reason"],
            "suggested_option": {
                "code": selected.get("code"),
                "strike": safe_float(selected.get("strike_price")),
                "delta": safe_float(selected.get("delta")),
                "gamma": safe_float(selected.get("gamma")),
                "vega": safe_float(selected.get("vega")),
                "theta": safe_float(selected.get("theta")),
                "iv": safe_float(selected.get("implied_volatility")),
                "expiry": selected.get("strike_time"),
                "days_to_expiry": safe_int(selected.get("days_to_expiry")),
                "otm_percent": round(safe_float(selected.get("otm_pct")) * 100, 2) if safe_float(selected.get("otm_pct")) is not None else None,
                "selection_score": round(safe_float(selected.get("selection_score")), 2) if safe_float(selected.get("selection_score")) is not None else None,
                "liquidity": assess_option_liquidity(selected, config),
            },
            "candidate_short_call": {
                "role": "candidate_from_option_chain_for_new_short_call_sale",
                "consider_selling": action_block["action"] in {"CONSIDER_SELL", "SELL_CALL", "SELL_CALL_WEAK"},
                "code": selected.get("code"),
                "strike": safe_float(selected.get("strike_price")),
                "delta": safe_float(selected.get("delta")),
                "gamma": safe_float(selected.get("gamma")),
                "vega": safe_float(selected.get("vega")),
                "theta": safe_float(selected.get("theta")),
                "iv": safe_float(selected.get("implied_volatility")),
                "expiry": selected.get("strike_time"),
                "days_to_expiry": safe_int(selected.get("days_to_expiry")),
                "otm_percent": round(safe_float(selected.get("otm_pct")) * 100, 2) if safe_float(selected.get("otm_pct")) is not None else None,
                "selection_score": round(safe_float(selected.get("selection_score")), 2) if safe_float(selected.get("selection_score")) is not None else None,
                "candidate_structure": action_block.get("candidate_structure"),
                "liquidity": assess_option_liquidity(selected, config),
                "event_block": event_block,
                "explanation_cn": "这是程序根据当前 option chain 挑出来、可考虑新增卖出的 short call 候选合约，不代表现有 short call。",
            },
            "decision_support": {
                "program_role": "PMCC risk and rolling management engine",
                "rule_priority_cn": "先识别现有 short call 风险，再判断是否适合收租，最后才推荐候选合约。",
                "preferred_dte_range": [config.preferred_dte_min, config.preferred_dte_max],
                "target_delta_range": [config.target_delta_low, config.target_delta_high],
                "coverage_scope": "per_underlying_only",
                "pmcc_rulebook": build_pmcc_rulebook(config),
                "greeks_available": bool(pd.to_numeric(enriched["delta"], errors="coerce").notna().any()),
                "data_quality": data_quality,
                "contracts_considered": int(len(enriched)),
                "manual_inputs_used": manual_inputs,
                "chain_iv_rank_proxy": chain_iv_rank_proxy,
                "chain_iv_rank_proxy_meta": chain_iv_rank_proxy_meta,
                "iv_snapshot": iv_snapshot,
                "iv_environment": iv_environment,
                "event_block": event_block,
            },
            "position_context": {
                "leaps_strike": config.leaps_strike,
                "short_call_strike": config.short_call_strike,
            },
            "roll_action": roll_action,
        }
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}
    finally:
        if owns_ctx and ctx is not None:
            ctx.close()


def analyze_pmcc_symbol(
    symbol: str,
    base_positions: List[PositionInput],
    short_calls: List[PositionInput],
    config: StrategyConfig,
    portfolio_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    short_call_legs = [item for item in short_calls if item.option_type == "CALL"]
    short_put_legs = [item for item in short_calls if item.option_type == "PUT"]
    other_short_legs = [item for item in short_calls if item.option_type not in {"CALL", "PUT"}]
    short_call_strike = short_call_legs[0].strike if short_call_legs else None
    local_config = StrategyConfig(
        leaps_strike=config.leaps_strike,
        short_call_strike=short_call_strike,
        target_delta_low=config.target_delta_low,
        target_delta_high=config.target_delta_high,
        history_bars=config.history_bars,
        preferred_dte_min=config.preferred_dte_min,
        preferred_dte_max=config.preferred_dte_max,
        iv_rank_sell_threshold=config.iv_rank_sell_threshold,
        iv_rank_avoid_threshold=config.iv_rank_avoid_threshold,
        high_iv_roll_threshold=config.high_iv_roll_threshold,
        roll_delta_warn=config.roll_delta_warn,
        roll_delta_danger=config.roll_delta_danger,
        roll_delta_critical=config.roll_delta_critical,
        roll_dte_attention=config.roll_dte_attention,
        roll_dte_active=config.roll_dte_active,
        roll_dte_urgent=config.roll_dte_urgent,
        roll_profit_take=config.roll_profit_take,
        roll_profit_strong=config.roll_profit_strong,
        iv_rank_override=config.iv_rank_override,
        iv_rank_overrides=config.iv_rank_overrides,
        hv_override=config.hv_override,
        iv_percentile_override=config.iv_percentile_override,
        iv_percentile_overrides=config.iv_percentile_overrides,
        iv_override=config.iv_override,
        trend_override=config.trend_override,
        enable_web_validation=config.enable_web_validation,
        allow_event_short_call=config.allow_event_short_call,
        earnings_block_days=config.earnings_block_days,
        event_block_days=config.event_block_days,
        ex_dividend_block_days=config.ex_dividend_block_days,
        min_candidate_open_interest=config.min_candidate_open_interest,
        min_candidate_volume=config.min_candidate_volume,
        max_candidate_bid_ask_spread_pct=config.max_candidate_bid_ask_spread_pct,
        max_candidate_last_mid_deviation_pct=config.max_candidate_last_mid_deviation_pct,
    )

    shared_ctx = OpenQuoteContext(host=HOST, port=PORT)
    call_market_data: Optional[Dict[str, Any]] = None
    try:
        call_market_data = load_call_market_data(symbol, shared_ctx, local_config)
        result = decision_engine(symbol, local_config, ctx=shared_ctx, call_market_data=call_market_data)
    except Exception as exc:
        result = {"symbol": symbol, "error": enrich_error_message(str(exc))}
    result["base_positions"] = [
        {"code": item.raw_code, "quantity": item.quantity, "strike": item.strike, "expiry": item.expiry, "cost_price": item.cost_price}
        for item in base_positions
    ]
    result["current_short_calls"] = [
        {"code": item.raw_code, "quantity": item.quantity, "strike": item.strike, "expiry": item.expiry, "cost_price": item.cost_price}
        for item in short_call_legs
    ]
    result["current_short_puts"] = [
        {"code": item.raw_code, "quantity": item.quantity, "strike": item.strike, "expiry": item.expiry, "cost_price": item.cost_price}
        for item in short_put_legs
    ]
    result["other_short_legs"] = [
        {"code": item.raw_code, "quantity": item.quantity, "strike": item.strike, "expiry": item.expiry, "cost_price": item.cost_price, "option_type": item.option_type}
        for item in other_short_legs
    ]
    result["coverage"] = {
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_call_contracts": sum(item.quantity for item in short_call_legs),
        "short_put_contracts": sum(item.quantity for item in short_put_legs),
        "other_short_contracts": sum(item.quantity for item in other_short_legs),
        "available_to_sell": max(sum(item.quantity for item in base_positions) - sum(item.quantity for item in short_call_legs), 0),
        "max_new_short_calls": max(sum(item.quantity for item in base_positions) - sum(item.quantity for item in short_call_legs), 0),
        "coverage_scope": "per_underlying_only",
        "coverage_scope_cn": "覆盖额度按同一标的分别计算，不能用其他标的的 LEAPS 混合覆盖。",
    }

    available_to_sell = result["coverage"]["available_to_sell"]
    if "candidate_short_call" in result:
        result["candidate_short_call"]["max_new_short_calls"] = available_to_sell
        result["candidate_short_call"]["coverage_scope"] = "per_underlying_only"
        result["candidate_short_call"]["coverage_scope_cn"] = "新增 short call 建议只对当前标的有效，且不能超过该标的剩余可覆盖张数。"
    if "error" not in result and available_to_sell <= 0:
        result["action"] = "WAIT"
        result.setdefault("reason", []).append("No additional short call capacity: current short calls already match LEAPS coverage.")
        if "candidate_short_call" in result:
            result["candidate_short_call"]["consider_selling"] = False
            result["candidate_short_call"]["explanation_cn"] = "当前 short call 数量已经达到 LEAPS 底仓可覆盖上限，暂不建议新增卖出。"
        result.setdefault("decision_support", {})["sell_capacity_blocked"] = True
        result["decision_support"]["sell_capacity_reason"] = "short_call_contracts >= base_contracts"

    if "error" not in result:
        try:
            enriched = call_market_data["enriched"].copy() if call_market_data is not None else pd.DataFrame()
            put_chain = get_option_chain_for_type(
                symbol,
                shared_ctx,
                OptionType.PUT,
                [item.expiry for item in short_put_legs if item.expiry],
            )
            if not put_chain.empty:
                put_greeks = get_greeks(put_chain["code"].tolist(), shared_ctx)
                put_enriched = enrich_put_options(put_chain, put_greeks, result["price"])
            else:
                put_enriched = pd.DataFrame()
            base_snapshot = get_greeks([item.raw_code for item in base_positions], shared_ctx)
            metric_options = pd.concat([enriched, put_enriched, base_snapshot], ignore_index=True)
        except Exception as exc:
            enriched = pd.DataFrame()
            put_enriched = pd.DataFrame()
            metric_options = pd.DataFrame()
            result.setdefault("decision_support", {}).setdefault("warnings", []).append(
                f"Position metric enrichment failed: {enrich_error_message(str(exc))}"
            )

        short_call_manual_inputs: Dict[str, Any] = {}
        if INTERACTIVE_MODE:
            for item in short_calls:
                short_call_manual_inputs[item.raw_code] = prompt_short_call_manual_metrics(item)

        long_leg_analysis = estimate_long_leg_metrics(base_positions, result["price"], metric_options)
        if portfolio_identity:
            long_leg_analysis.update({key: value for key, value in portfolio_identity.items() if value is not None})
        result["long_leg_analysis"] = long_leg_analysis
        result["iv_environment"] = build_iv_environment(
            result.get("iv"),
            result.get("hv"),
            result.get("iv_rank_est"),
            result.get("iv_percentile"),
            result.get("candidate_short_call"),
            long_leg_analysis,
        )
        result.setdefault("decision_support", {})["iv_environment"] = result["iv_environment"]

        candidate = result.get("candidate_short_call", {})
        candidate_strike = safe_float(candidate.get("strike"))
        slot_analysis = build_leaps_coverage_slot_analysis(long_leg_analysis, short_call_legs, candidate_strike)
        result["leaps_coverage_slots"] = slot_analysis
        result["coverage"]["eligible_new_short_call_slots"] = slot_analysis.get("eligible_new_short_call_slots")
        if candidate and candidate_strike is not None:
            eligible_slots = safe_int(slot_analysis.get("eligible_new_short_call_slots")) or 0
            candidate["leaps_coverage_slot_analysis"] = slot_analysis
            candidate["max_new_short_calls"] = min(available_to_sell, eligible_slots)
            candidate["mcmillan_minimum_safe_short_strike"] = slot_analysis.get("minimum_candidate_safe_strike")
            candidate["mcmillan_max_unoccupied_safe_strike"] = slot_analysis.get("max_unoccupied_safe_strike")
            if eligible_slots <= 0:
                required_strike = safe_float(slot_analysis.get("minimum_candidate_safe_strike")) or safe_float(slot_analysis.get("max_unoccupied_safe_strike"))
                candidate["mcmillan_safety"] = "BLOCKED_NO_ELIGIBLE_LEAPS_SLOT"
                candidate["consider_selling"] = False
                safe_alternative = find_mcmillan_safe_alternative(enriched, local_config, required_strike)
                if not safe_alternative.empty:
                    alternative = safe_alternative.iloc[0]
                    candidate["mcmillan_safe_alternative"] = {
                        "code": alternative.get("code"),
                        "strike": safe_float(alternative.get("strike_price")),
                        "delta": safe_float(alternative.get("delta")),
                        "gamma": safe_float(alternative.get("gamma")),
                        "vega": safe_float(alternative.get("vega")),
                        "theta": safe_float(alternative.get("theta")),
                        "iv": safe_float(alternative.get("implied_volatility")),
                        "expiry": alternative.get("strike_time"),
                        "days_to_expiry": safe_int(alternative.get("days_to_expiry")),
                        "otm_percent": round(safe_float(alternative.get("otm_pct")) * 100, 2) if safe_float(alternative.get("otm_pct")) is not None else None,
                        "selection_score": round(safe_float(alternative.get("selection_score")), 2) if safe_float(alternative.get("selection_score")) is not None else None,
                    }
                else:
                    candidate["mcmillan_safe_alternative"] = None
                if available_to_sell > 0:
                    result["action"] = "AVOID_SELL"
                result.setdefault("reason", []).append(
                    f"Candidate short strike {candidate_strike:.2f} has no unoccupied LEAPS coverage slot above its cost-line guard."
                )
            else:
                candidate["mcmillan_safety"] = "OK_HAS_ELIGIBLE_LEAPS_SLOTS"

        if long_leg_analysis.get("warnings"):
            result.setdefault("reason", []).extend(long_leg_analysis["warnings"])

        result["short_call_reviews"] = [
            analyze_short_call_position(
                item,
                result["price"],
                enriched,
                local_config,
                result.get("iv_rank_est"),
                base_positions,
                short_call_manual_inputs.get(item.raw_code),
            )
            for item in short_call_legs
        ]
        for item in result["short_call_reviews"]:
            item["roll_candidates"] = find_roll_candidates_for_short_call(
                item,
                enriched,
                result["price"],
                local_config,
                long_leg_analysis,
                portfolio_identity,
            )
        slot_analysis = enrich_leaps_slot_iv_spreads(
            slot_analysis,
            result["short_call_reviews"],
            result.get("candidate_short_call", {}),
        )
        result["leaps_coverage_slots"] = slot_analysis
        result["slot_iv_spread_analysis"] = slot_analysis
        result["iv_environment"] = build_iv_environment(
            result.get("iv"),
            result.get("hv"),
            result.get("iv_rank_est"),
            result.get("iv_percentile"),
            result.get("candidate_short_call"),
            long_leg_analysis,
            slot_analysis,
        )
        result.setdefault("decision_support", {})["iv_environment"] = result["iv_environment"]
        result["decision_support"]["slot_iv_spread_analysis"] = slot_analysis
        if candidate:
            candidate["leaps_coverage_slot_analysis"] = slot_analysis
        result["short_put_reviews"] = [
            analyze_short_put_position(
                item,
                result["price"],
                put_enriched,
                result.get("iv_rank_est"),
            )
            for item in short_put_legs
        ]
        for item in result["short_put_reviews"]:
            item["roll_candidates"] = find_roll_candidates_for_short_put(
                item,
                put_enriched,
                result["price"],
                local_config,
            )
        result["spread_other_reviews"] = [
            {
                "code": item.raw_code,
                "strategy_type": "spread_or_other",
                "quantity": item.quantity,
                "option_type": item.option_type,
                "action": "UNSUPPORTED",
                "reason": ["Spread/other short-leg handling is reserved for a future module."],
            }
            for item in other_short_legs
        ]
        if short_call_manual_inputs:
            result.setdefault("decision_support", {})["short_call_manual_inputs"] = short_call_manual_inputs
        short_vega = 0.0
        for item in result["short_call_reviews"]:
            item_vega = safe_float(item.get("vega"))
            if item_vega is not None:
                short_vega += item_vega * max(safe_int(item.get("quantity")) or 0, 0)
        result["diagonal_risk"] = {
            "net_vega_est": round((safe_float(long_leg_analysis.get("net_long_vega")) or 0.0) - short_vega, 4),
            "iv_crush_risk": "HIGH_LONG_VEGA" if (safe_float(long_leg_analysis.get("net_long_vega")) or 0.0) - short_vega > 1 else "LOW_OR_UNKNOWN",
            "mcmillan_notes": [
                "PMCC is treated as a long call diagonal spread.",
                "Prefer long calls with delta >= 0.80 and low extrinsic value.",
                "Avoid selling or rolling short calls below the long-leg cost-line guard.",
            ],
        }
        result["passarelli_greeks_management"] = build_passarelli_greeks_management(
            long_leg_analysis,
            result["short_call_reviews"],
            result.get("candidate_short_call", {}),
            result["diagonal_risk"],
        )
        result.setdefault("decision_support", {})["max_new_short_calls"] = result["coverage"]["max_new_short_calls"]
        result["decision_support"]["coverage_scope"] = "per_underlying_only"
        result["decision_support"]["coverage_scope_cn"] = "覆盖额度按同一标的分别计算，新增 short call 建议不能超过该标的剩余可覆盖张数。"
        result["summary_cn"] = build_chinese_summary_clean(result)
        if result.get("coverage", {}).get("available_to_sell") == 0:
            result["summary_cn"] += " 当前 short call 数量已达到 LEAPS 底仓可覆盖上限，暂不建议新增卖出 short call。"

    shared_ctx.close()
    return result


def find_mcmillan_safe_alternative(
    enriched: pd.DataFrame,
    config: StrategyConfig,
    required_strike: Optional[float],
) -> pd.DataFrame:
    if enriched.empty or required_strike is None:
        return pd.DataFrame()

    scored_safe = score_options(enriched, config)
    strikes = pd.to_numeric(scored_safe["strike_price"], errors="coerce")
    dtes = pd.to_numeric(scored_safe["days_to_expiry"], errors="coerce")
    is_otm = scored_safe["is_otm"].fillna(False).astype(bool) if "is_otm" in scored_safe.columns else pd.Series(False, index=scored_safe.index)
    safe_alternative = scored_safe[
        (strikes >= required_strike)
        & is_otm
        & scored_safe["liquidity_ok"]
        & dtes.between(config.preferred_dte_min, config.preferred_dte_max, inclusive="both")
    ].copy()
    if safe_alternative.empty:
        return safe_alternative

    safe_alternative["mcmillan_strike_gap"] = (
        pd.to_numeric(safe_alternative["strike_price"], errors="coerce") - required_strike
    ).abs()
    return safe_alternative.sort_values(
        by=["mcmillan_strike_gap", "selection_score", "strike_price"],
        ascending=[True, False, True],
    )


def git_output(args: List[str]) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def build_report_version_info() -> Dict[str, str]:
    branch = git_output(["branch", "--show-current"]) or "unknown"
    commit = git_output(["rev-parse", "--short", "HEAD"]) or "unknown"
    return {
        "branch": branch,
        "version": commit,
    }


def render_html_report(result: Dict[str, Any]) -> str:
    return render_html_report_from_reports(result, report_version=build_report_version_info())


def write_html_report(result: Dict[str, Any]) -> Path:
    report_path = Path(__file__).with_name("pmcc_report.html")
    return write_html_report_to_path(report_path, result, report_version=build_report_version_info())


def format_terminal_link(path: Path) -> str:
    uri = path.resolve().as_uri()
    return f"\033]8;;{uri}\033\\{uri}\033]8;;\033\\"


def record_trade_journal_event(raw_event: str, path: Path = TRADE_JOURNAL_FILE) -> Dict[str, Any]:
    event = parse_trade_event_text(raw_event)
    issues = validate_trade_event(event)
    print(json.dumps(event, indent=2, ensure_ascii=False, default=str))
    if issues:
        raise ValueError("Trade event validation failed: " + "; ".join(issues))

    confirmation = read_interactive_text("Save this trade event? [y/N]: ").lower()
    if confirmation not in {"y", "yes"}:
        return {"saved": False, "event": event, "path": str(path)}

    saved = append_trade_event(path, event)
    return {"saved": True, "event": saved, "path": str(path)}


def import_schwab_trade_csv(path: Path, journal_path: Path = TRADE_JOURNAL_FILE) -> Dict[str, Any]:
    drafts = parse_schwab_trade_csv_text(path.read_text(encoding="utf-8-sig"))
    print(json.dumps(drafts, indent=2, ensure_ascii=False, default=str))
    valid_events = [item["event"] for item in drafts if not item["issues"]]
    invalid_count = len(drafts) - len(valid_events)
    if not valid_events:
        return {"saved": 0, "invalid": invalid_count, "path": str(journal_path)}

    confirmation = read_interactive_text(f"Save {len(valid_events)} valid imported trade event(s)? [y/N]: ").lower()
    if confirmation not in {"y", "yes"}:
        return {"saved": 0, "invalid": invalid_count, "path": str(journal_path)}

    for event in valid_events:
        append_trade_event(journal_path, event)
    return {"saved": len(valid_events), "invalid": invalid_count, "path": str(journal_path)}


def render_trade_journal_obsidian_note(journal_path: Path = TRADE_JOURNAL_FILE, reflection: str = "") -> str:
    return build_obsidian_trade_note(read_trade_events(journal_path), reflection)


def split_pmcc_result(result: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    stage_1_keys = [
        "symbol",
        "price",
        "quote_source",
        "data_quality",
        "data_validation",
        "event_block",
        "trend",
        "iv_rank_est",
        "iv_percentile",
        "iv_rank_analysis",
        "iv",
        "hv",
        "iv_snapshot",
        "iv_environment",
        "base_positions",
        "current_short_calls",
        "current_short_puts",
        "other_short_legs",
        "coverage",
        "long_leg_analysis",
        "leaps_coverage_slots",
        "slot_iv_spread_analysis",
        "diagonal_risk",
        "decision_support",
    ]
    stage_2_keys = [
        "symbol",
        "action",
        "reason",
        "candidate_short_call",
        "passarelli_greeks_management",
        "short_call_reviews",
        "short_put_reviews",
        "spread_other_reviews",
        "summary_cn",
    ]
    position_analysis = {key: result[key] for key in stage_1_keys if key in result}
    operation_recommendation = {key: result[key] for key in stage_2_keys if key in result}
    if "error" in result:
        position_analysis["error"] = result["error"]
        operation_recommendation["error"] = result["error"]
    return position_analysis, operation_recommendation


def build_pmcc_two_stage_result(
    source_metadata: Dict[str, Any],
    base_positions: List[PositionInput],
    short_calls: List[PositionInput],
    config: StrategyConfig,
    memory_save_base_positions: Optional[List[PositionInput]] = None,
    memory_save_short_calls: Optional[List[PositionInput]] = None,
    memory_scope: str = "analysis_snapshot",
    save_memory: bool = True,
) -> Dict[str, Any]:
    print_position_inventory_before_analysis(base_positions, short_calls)
    if save_memory:
        save_position_memory(
            memory_save_base_positions if memory_save_base_positions is not None else base_positions,
            memory_save_short_calls if memory_save_short_calls is not None else short_calls,
            memory_scope,
        )
    grouped_bases = group_positions_by_underlying(base_positions)
    grouped_shorts = group_positions_by_underlying(short_calls)
    symbols = sorted(set(grouped_bases) | set(grouped_shorts))
    iv_rank_metadata = build_iv_rank_input_metadata(symbols, config)
    source_metadata = {
        **source_metadata,
        "iv_rank_memory": iv_rank_metadata,
    }

    stage_1: List[Dict[str, Any]] = []
    stage_2: List[Dict[str, Any]] = []
    portfolio_id = safe_text(source_metadata.get("portfolio_id"))
    portfolio_label = safe_text(source_metadata.get("portfolio_label"))
    for symbol in symbols:
        portfolio_identity = {
            "broker": portfolio_id,
            "account": safe_text(source_metadata.get("account")),
            "portfolio_id": portfolio_id,
            "portfolio_label": portfolio_label,
            "symbol": symbol,
        }
        result = analyze_pmcc_symbol(
            symbol,
            grouped_bases.get(symbol, []),
            grouped_shorts.get(symbol, []),
            config,
            portfolio_identity,
        )
        position_analysis, operation_recommendation = split_pmcc_result(result)
        if portfolio_id is not None:
            position_analysis["portfolio_id"] = portfolio_id
            operation_recommendation["portfolio_id"] = portfolio_id
        if portfolio_label is not None:
            position_analysis["portfolio_label"] = portfolio_label
            operation_recommendation["portfolio_label"] = portfolio_label
        stage_1.append(position_analysis)
        stage_2.append(operation_recommendation)

    return {
        "mode": "pmcc_two_stage",
        "position_source": source_metadata,
        "recorded_positions": {
            "base_positions": [position_to_record(item) for item in base_positions],
            "short_calls": [position_to_record(item) for item in short_calls],
            "memory_file": str(MEMORY_FILE),
            "portfolio_id": portfolio_id,
            "portfolio_label": portfolio_label,
        },
        "stage_1_position_analysis": stage_1,
        "stage_2_operation_recommendations": stage_2,
    }


def build_pmcc_multi_portfolio_result(
    source_metadata: Dict[str, Any],
    portfolios: List[Dict[str, Any]],
    config: StrategyConfig,
) -> Dict[str, Any]:
    stage_1: List[Dict[str, Any]] = []
    stage_2: List[Dict[str, Any]] = []
    recorded_portfolios: List[Dict[str, Any]] = []
    portfolio_sources: List[Dict[str, Any]] = []

    for portfolio in portfolios:
        portfolio_base_positions = portfolio.get("base_positions") or []
        portfolio_short_calls = portfolio.get("short_calls") or []
        portfolio_metadata = {
            "source": portfolio.get("source"),
            "portfolio_id": portfolio.get("portfolio_id"),
            "portfolio_label": portfolio.get("portfolio_label"),
        }
        child = build_pmcc_two_stage_result(
            portfolio_metadata,
            portfolio_base_positions,
            portfolio_short_calls,
            config,
            save_memory=False,
        )
        stage_1.extend(child["stage_1_position_analysis"])
        stage_2.extend(child["stage_2_operation_recommendations"])
        recorded_portfolios.append(
            {
                "portfolio_id": portfolio.get("portfolio_id"),
                "portfolio_label": portfolio.get("portfolio_label"),
                "base_positions": [position_to_record(item) for item in portfolio_base_positions],
                "short_calls": [position_to_record(item) for item in portfolio_short_calls],
            }
        )
        portfolio_sources.append(child["position_source"])

    all_base_positions = [item for portfolio in portfolios for item in (portfolio.get("base_positions") or [])]
    all_short_calls = [item for portfolio in portfolios for item in (portfolio.get("short_calls") or [])]
    short_call_legs = [item for item in all_short_calls if item.option_type == "CALL"]
    short_put_legs = [item for item in all_short_calls if item.option_type == "PUT"]
    other_short_legs = [item for item in all_short_calls if item.option_type not in {"CALL", "PUT"}]

    return {
        "mode": "pmcc_two_stage_multi_portfolio",
        "position_source": {
            **source_metadata,
            "portfolio_isolation": "broker_account",
            "portfolio_sources": portfolio_sources,
            "total_base_contracts": sum(item.quantity for item in all_base_positions),
            "total_short_contracts": sum(item.quantity for item in all_short_calls),
            "total_short_call_contracts": sum(item.quantity for item in short_call_legs),
            "total_short_put_contracts": sum(item.quantity for item in short_put_legs),
            "total_other_short_contracts": sum(item.quantity for item in other_short_legs),
        },
        "recorded_positions": {
            "portfolios": recorded_portfolios,
            "base_positions": [position_to_record(item) for item in all_base_positions],
            "short_calls": [position_to_record(item) for item in all_short_calls],
            "memory_file": str(MEMORY_FILE),
            "memory_scope": "broker_isolated_snapshots",
        },
        "stage_1_position_analysis": stage_1,
        "stage_2_operation_recommendations": stage_2,
    }


def run_pmcc_interactive(config: StrategyConfig) -> Dict[str, Any]:
    base_positions, short_calls, metadata = collect_positions_interactive()
    return build_pmcc_two_stage_result(metadata, base_positions, short_calls, config)


def run_pmcc_opend(
    config: StrategyConfig,
    external_base_raw: str = "",
    external_short_raw: str = "",
    schwab_remove_base_raw: str = "",
    schwab_remove_short_raw: str = "",
    schwab_import_positions_path: str = "",
    prompt_external: bool = True,
) -> Dict[str, Any]:
    previous_futu_base_positions, previous_futu_short_calls, previous_futu_metadata = load_broker_positions(FUTU_POSITIONS_FILE)
    schwab_base_positions, schwab_short_calls, schwab_metadata = load_broker_positions(SCHWAB_POSITIONS_FILE)
    opend_base_positions, opend_short_calls, opend_metadata = collect_positions_from_opend()
    futu_position_diff = {
        "base_positions": diff_positions_by_code(previous_futu_base_positions, opend_base_positions),
        "short_positions": diff_positions_by_code(previous_futu_short_calls, opend_short_calls),
    }
    save_broker_positions(FUTU_POSITIONS_FILE, "futu_opend_snapshot", opend_base_positions, opend_short_calls)

    if schwab_import_positions_path.strip():
        schwab_base_positions, schwab_short_calls, schwab_change_metadata = parse_tos_position_statement(
            Path(schwab_import_positions_path)
        )
        schwab_change_metadata = {
            **schwab_change_metadata,
            "mode": "replace_from_thinkorswim_position_statement",
        }
    else:
        if external_base_raw.strip() or external_short_raw.strip() or not prompt_external:
            external_base_positions, external_short_calls, external_metadata = collect_external_positions_from_args(
                external_base_raw,
                external_short_raw,
            )
        else:
            external_base_positions, external_short_calls, external_metadata = collect_external_positions_interactive()
        schwab_remove_base_positions = parse_positions_input(schwab_remove_base_raw) if schwab_remove_base_raw.strip() else []
        schwab_remove_short_calls = parse_positions_input(schwab_remove_short_raw) if schwab_remove_short_raw.strip() else []
        schwab_base_positions, schwab_short_calls, schwab_change_metadata = apply_schwab_position_changes(
            schwab_base_positions,
            schwab_short_calls,
            external_base_positions,
            external_short_calls,
            schwab_remove_base_positions,
            schwab_remove_short_calls,
        )
        if not external_base_raw.strip() and not external_short_raw.strip() and not schwab_remove_base_raw.strip() and not schwab_remove_short_raw.strip():
            schwab_change_metadata = {
                **schwab_change_metadata,
                "mode": "reuse_saved_schwab_positions",
            }
    if schwab_import_positions_path.strip():
        external_metadata = {
            "source": "skipped_for_schwab_import",
            "base_contracts": 0,
            "short_call_contracts": 0,
        }
    save_broker_positions(SCHWAB_POSITIONS_FILE, "schwab_manual_positions", schwab_base_positions, schwab_short_calls)
    print_broker_position_confirmation(
        "Schwab",
        schwab_base_positions,
        schwab_short_calls,
        {**schwab_metadata, **schwab_change_metadata, "path": schwab_change_metadata.get("path") or schwab_metadata.get("path")},
    )

    base_merge_metadata = {
        "sources": ["futu_opend", "schwab_manual"],
        "futu_contracts": sum(item.quantity for item in opend_base_positions),
        "schwab_contracts": sum(item.quantity for item in schwab_base_positions),
        "total_contracts": sum(item.quantity for item in opend_base_positions) + sum(item.quantity for item in schwab_base_positions),
        "analysis_policy": "not_merged_for_pmcc_coverage",
    }
    short_merge_metadata = {
        "sources": ["futu_opend", "schwab_manual"],
        "futu_contracts": sum(item.quantity for item in opend_short_calls),
        "schwab_contracts": sum(item.quantity for item in schwab_short_calls),
        "total_contracts": sum(item.quantity for item in opend_short_calls) + sum(item.quantity for item in schwab_short_calls),
        "analysis_policy": "not_merged_for_pmcc_coverage",
    }
    all_base_positions = opend_base_positions + schwab_base_positions
    all_short_calls = opend_short_calls + schwab_short_calls
    merged_short_calls = [item for item in all_short_calls if item.option_type == "CALL"]
    merged_short_puts = [item for item in all_short_calls if item.option_type == "PUT"]
    merged_other_shorts = [item for item in all_short_calls if item.option_type not in {"CALL", "PUT"}]
    metadata = {
        "source": "futu_opend_plus_schwab_manual",
        "analysis_policy": "broker_account_isolated",
        "futu_snapshot": previous_futu_metadata,
        "futu_position_diff": futu_position_diff,
        "schwab": schwab_metadata,
        "schwab_changes": schwab_change_metadata,
        "opend": opend_metadata,
        "external": {**external_metadata, "meaning": "schwab_additions"},
        "merge": {
            "base_positions": base_merge_metadata,
            "short_calls": short_merge_metadata,
        },
        "total_base_contracts": sum(item.quantity for item in all_base_positions),
        "total_short_contracts": sum(item.quantity for item in all_short_calls),
        "total_short_call_contracts": sum(item.quantity for item in merged_short_calls),
        "total_short_put_contracts": sum(item.quantity for item in merged_short_puts),
        "total_other_short_contracts": sum(item.quantity for item in merged_other_shorts),
    }
    return build_pmcc_multi_portfolio_result(
        metadata,
        [
            {
                "portfolio_id": "FUTU",
                "portfolio_label": "Futu OpenD",
                "source": "futu_opend",
                "base_positions": opend_base_positions,
                "short_calls": opend_short_calls,
            },
            {
                "portfolio_id": "SCHWAB",
                "portfolio_label": "Schwab",
                "source": "schwab_manual",
                "base_positions": schwab_base_positions,
                "short_calls": schwab_short_calls,
            },
        ],
        config,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate PMCC / covered call opportunities using Futu option data.")
    parser.add_argument("symbol", nargs="?", default="US.NVDA", help="Ticker in Futu format, e.g. US.NVDA")
    parser.add_argument("--host", default=HOST, help="Futu OpenD host")
    parser.add_argument("--port", type=int, default=PORT, help="Futu OpenD port")
    parser.add_argument("--leaps-strike", type=float, default=DEFAULT_CONFIG.leaps_strike)
    parser.add_argument("--short-call-strike", type=float, default=DEFAULT_CONFIG.short_call_strike)
    parser.add_argument("--target-delta-low", type=float, default=DEFAULT_CONFIG.target_delta_low)
    parser.add_argument("--target-delta-high", type=float, default=DEFAULT_CONFIG.target_delta_high)
    parser.add_argument("--preferred-dte-min", type=int, default=DEFAULT_CONFIG.preferred_dte_min)
    parser.add_argument("--preferred-dte-max", type=int, default=DEFAULT_CONFIG.preferred_dte_max)
    parser.add_argument("--iv-rank", type=float, default=None, help="Manual IV Rank override for all symbols, e.g. 68")
    parser.add_argument("--iv-rank-overrides", default="", help="Global or per-symbol historical IV Rank overrides, e.g. 53 or US.NVDA=63,US.MSFT=45")
    parser.add_argument("--iv-percentile", type=float, default=None, help="Manual IV Percentile override, e.g. 69")
    parser.add_argument("--iv-percentile-overrides", default="", help="Global or per-symbol IV Percentile overrides, e.g. 77 or US.NVDA=77,US.MSFT=21")
    parser.add_argument("--iv", type=float, default=None, help="Manual underlying IV override in percent, e.g. 48.51")
    parser.add_argument("--hv", type=float, default=None, help="Manual historical volatility override in percent, e.g. 33.2")
    parser.add_argument("--trend", choices=sorted(VALID_TRENDS), default=None, help="Manual trend override")
    parser.add_argument("--interactive", action="store_true", help="Prompt for missing trend / IV / Greeks")
    parser.add_argument("--pmcc-interactive", action="store_true", help="Interactive PMCC workflow for multiple base and short call positions")
    parser.add_argument("--pmcc-opend", action="store_true", help="Read real US option positions from Futu OpenD and run the two-stage PMCC workflow")
    parser.add_argument("--external-base", default="", help="Additional non-Futu LEAPS/base calls in CODE,QTY or CODE,QTY,COST format, separated by ';'")
    parser.add_argument("--external-short", default="", help="Additional non-Futu short calls in CODE,QTY or CODE,QTY,COST format, separated by ';'")
    parser.add_argument("--schwab-add-base", default="", help="Alias for --external-base; add Schwab LEAPS/base calls")
    parser.add_argument("--schwab-add-short", default="", help="Alias for --external-short; add Schwab short options")
    parser.add_argument("--schwab-remove-base", default="", help="Remove Schwab LEAPS/base calls in CODE,QTY format, separated by ';'")
    parser.add_argument("--schwab-remove-short", default="", help="Remove Schwab short options in CODE,QTY format, separated by ';'")
    parser.add_argument("--schwab-import-positions", default="", help="Replace Schwab positions from a thinkorswim Position Statement CSV/TXT export")
    parser.add_argument("--trade-journal-event", default="", help="Parse and optionally save one trade journal event as key=value pairs separated by ';'")
    parser.add_argument("--trade-journal-import-schwab-csv", default="", help="Parse a Schwab/thinkorswim trade CSV into reviewed trade journal drafts")
    parser.add_argument("--trade-journal-obsidian-note", action="store_true", help="Print an Obsidian Markdown trade-review draft from the local journal")
    parser.add_argument("--trade-journal-reflection", default="", help="Reflection text to include in the Obsidian Markdown draft")
    parser.add_argument("--trade-journal-path", default=str(TRADE_JOURNAL_FILE), help="Local append-only trade journal JSONL path; defaults outside Git under PMCC_DATA_DIR or ~/.pmcc")
    parser.add_argument("--no-external-prompt", action="store_true", help="Skip prompting for additional non-Futu positions")
    parser.add_argument("--no-web-validation", action="store_true", help="Disable Yahoo Finance and NextEarningsDate backup validation")
    args = parser.parse_args()
    global_iv_rank, args.iv_rank_overrides_map = parse_iv_override_input(args.iv_rank_overrides, "IV Rank")
    global_iv_percentile, args.iv_percentile_overrides_map = parse_iv_override_input(args.iv_percentile_overrides, "IV Percentile")
    if args.iv_rank is None and global_iv_rank is not None:
        args.iv_rank = global_iv_rank
    if args.iv_percentile is None and global_iv_percentile is not None:
        args.iv_percentile = global_iv_percentile
    return args


def main() -> None:
    global HOST, PORT, INTERACTIVE_MODE

    args = parse_args()
    HOST = args.host
    PORT = args.port
    INTERACTIVE_MODE = args.interactive or args.pmcc_interactive

    if args.trade_journal_event:
        result = record_trade_journal_event(args.trade_journal_event, Path(args.trade_journal_path))
        if result["saved"]:
            print(f"Trade event saved to {Path(result['path']).resolve()}")
        else:
            print("Trade event was not saved.")
        return

    if args.trade_journal_import_schwab_csv:
        result = import_schwab_trade_csv(Path(args.trade_journal_import_schwab_csv), Path(args.trade_journal_path))
        print(
            f"Schwab CSV import complete: saved={result['saved']}, "
            f"invalid={result['invalid']}, journal={Path(result['path']).resolve()}"
        )
        return

    if args.trade_journal_obsidian_note:
        print(render_trade_journal_obsidian_note(Path(args.trade_journal_path), args.trade_journal_reflection))
        return

    config = StrategyConfig(
        leaps_strike=args.leaps_strike,
        short_call_strike=args.short_call_strike,
        target_delta_low=args.target_delta_low,
        target_delta_high=args.target_delta_high,
        preferred_dte_min=args.preferred_dte_min,
        preferred_dte_max=args.preferred_dte_max,
        iv_rank_override=args.iv_rank,
        iv_rank_overrides=args.iv_rank_overrides_map,
        iv_percentile_override=args.iv_percentile,
        iv_percentile_overrides=args.iv_percentile_overrides_map,
        iv_override=args.iv,
        hv_override=args.hv,
        trend_override=args.trend,
        enable_web_validation=not args.no_web_validation,
    )

    if args.pmcc_opend:
        print("")
        print("重要提醒：请先启动 Futu OpenD，完成登录，并保持 OpenD 正在运行。")
        print(f"程序将通过 {HOST}:{PORT} 连接 Futu OpenD 读取持仓和行情；如果 OpenD 未启动，后续会连接失败。")
        print("Running Futu OpenD startup data-quality check on US.NVDA...")
        preflight = run_futu_opend_preflight("US.NVDA", config)
        print(
            "Futu OpenD startup check OK: "
            f"price={preflight.get('last_price')}, "
            f"expiries={preflight.get('expiry_count')}, "
            f"sampled_contracts={preflight.get('chain_contracts_sampled')}, "
            f"quality={preflight.get('data_quality', {}).get('status')}"
        )
        print("")
        result = run_pmcc_opend(
            config,
            external_base_raw=args.schwab_add_base or args.external_base,
            external_short_raw=args.schwab_add_short or args.external_short,
            schwab_remove_base_raw=args.schwab_remove_base,
            schwab_remove_short_raw=args.schwab_remove_short,
            schwab_import_positions_path=args.schwab_import_positions,
            prompt_external=False,
        )
    elif args.pmcc_interactive:
        result = run_pmcc_interactive(config)
    else:
        result = decision_engine(args.symbol, config)

    report_path = write_html_report(result)
    print(format_plain_text_report(result))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("")
    print(f"HTML报告已生成：{format_terminal_link(report_path)}")
    print(f"报告路径：{report_path.resolve()}")


if __name__ == "__main__":
    main()
