from typing import Any, Callable, Dict, List, Optional, Tuple

from pmcc.constants import VALID_TRENDS
from pmcc.positions import parse_positions_input
from pmcc.utils import safe_float, symbol_to_web_ticker


def read_interactive_text(prompt: str) -> str:
    return input(prompt).strip()


def write_interactive_message(message: str) -> None:
    print(message)


def prompt_required_text(label: str) -> str:
    while True:
        raw = read_interactive_text(f"{label}: ")
        if raw:
            return raw
        write_interactive_message("不能为空。请重新输入。")


def prompt_optional_float(label: str) -> Optional[float]:
    while True:
        raw = read_interactive_text(f"{label} (press Enter to skip): ")
        if raw == "":
            return None
        try:
            return float(raw)
        except ValueError:
            write_interactive_message("请输入数字；如果暂时没有数据，直接回车跳过。")


def prompt_confirm_or_override_float(label: str, current_value: Optional[float]) -> Optional[float]:
    while True:
        if current_value is None:
            raw = read_interactive_text(f"{label} (press Enter to skip): ")
        else:
            raw = read_interactive_text(f"{label} [current {current_value:.2f}, press Enter to confirm]: ")
        if raw == "":
            return current_value
        try:
            return float(raw)
        except ValueError:
            write_interactive_message("请输入数字；如果沿用当前值，直接回车。")


def prompt_optional_trend(label: str) -> Optional[str]:
    while True:
        raw = read_interactive_text(f"{label} [UP/DOWN/FLAT] (press Enter to skip): ").upper()
        if raw == "":
            return None
        if raw in VALID_TRENDS:
            return raw
        write_interactive_message("请输入 UP、DOWN、FLAT；如果暂时不确定，直接回车跳过。")


def prompt_confirm_or_override_trend(label: str, current_value: str) -> str:
    while True:
        raw = read_interactive_text(f"{label} [current {current_value}, UP/DOWN/FLAT, press Enter to confirm]: ").upper()
        if raw == "":
            return current_value
        if raw in VALID_TRENDS:
            return raw
        write_interactive_message("请输入 UP、DOWN、FLAT；如果沿用当前值，直接回车。")


def prompt_positions_with_memory(label: str, memory_value: str, allow_empty: bool = False):
    if memory_value:
        write_interactive_message(f"上次确认的{label}：{memory_value}")
        write_interactive_message("如果这次和上次相同，直接回车确认；如果有变化，请输入新的内容。")
    else:
        write_interactive_message(f"本地记忆中还没有{label}记录，请直接输入。")

    while True:
        raw = read_interactive_text(f"{label}: ")
        chosen = raw or memory_value
        if not chosen and allow_empty:
            return []
        if not chosen:
            write_interactive_message("不能为空，请重新输入。")
            continue
        try:
            return parse_positions_input(chosen)
        except ValueError as exc:
            write_interactive_message(str(exc))


def prompt_optional_positions(label: str):
    write_interactive_message(f"Enter additional {label} from other brokers, or press Enter to skip.")
    write_interactive_message("Format: CODE,QTY or CODE,QTY,COST; separate multiple positions with ';'.")
    write_interactive_message("Example: US.MSFT270319C360000,1,95.5")
    while True:
        try:
            raw = read_interactive_text(f"additional {label}: ")
        except EOFError:
            return []
        if not raw:
            return []
        try:
            return parse_positions_input(raw)
        except ValueError as exc:
            write_interactive_message(str(exc))


def collect_positions_interactive(memory):
    write_interactive_message("Enter LEAPS / base call positions as CODE,QTY;CODE,QTY")
    write_interactive_message("Example: US.NVDA270117C140000,2;US.AAPL270117C150000,1")
    write_interactive_message("Quantity is number of option contracts, not shares.")
    base_positions = prompt_positions_with_memory("base positions", memory.get("base_positions", ""))

    write_interactive_message("Enter current short calls as CODE,QTY;CODE,QTY")
    write_interactive_message("Example: US.NVDA260522C225000,1")
    write_interactive_message("Press Enter if there is no current short call.")
    short_calls = prompt_positions_with_memory("current short calls", memory.get("short_calls", ""), allow_empty=True)

    metadata = {
        "source": "interactive",
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_call_contracts": sum(item.quantity for item in short_calls),
    }
    return base_positions, short_calls, metadata


def collect_external_positions_interactive():
    base_positions = prompt_optional_positions("LEAPS / base call positions")
    short_calls = prompt_optional_positions("short calls")
    return base_positions, short_calls, {
        "source": "external_interactive",
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_call_contracts": sum(item.quantity for item in short_calls),
    }


def prompt_short_call_manual_metrics(short_call):
    write_interactive_message(f"\n当前正在补充 short call 数据: {short_call.raw_code}")
    write_interactive_message("delta 说明：")
    write_interactive_message("- 输入这张 short call 当前的 delta")
    write_interactive_message("- 示例：0.3173")
    manual_delta = prompt_optional_float("请输入 short call delta")
    write_interactive_message("profit captured 说明：")
    write_interactive_message("- 输入这张 short call 已经赚到的利润，占最初最大权利金的百分比")
    write_interactive_message("- 例如已经赚了一半，就输入 50")
    write_interactive_message("- 例如已经赚了 70%，就输入 70")
    profit_capture = prompt_optional_float("请输入 profit captured 百分比")
    write_interactive_message("IV 说明：")
    write_interactive_message("- 输入这张 short call 当前的 implied volatility")
    write_interactive_message("- 示例：43.7")
    manual_iv = prompt_optional_float("请输入 short call IV")
    return {
        "delta": manual_delta,
        "profit_capture_pct": profit_capture,
        "iv": manual_iv,
    }


def prompt_iv_rank_value(symbol: str, current_value: Optional[float]) -> Optional[float]:
    label = f"{symbol} 当前 IV Rank"
    try:
        return prompt_confirm_or_override_float(label, current_value)
    except EOFError:
        return current_value


def collect_iv_rank_overrides_for_symbols(
    symbols: List[str],
    existing_overrides: Dict[str, float],
    memory: Dict[str, Dict[str, Any]],
    memory_file: str,
    symbol_override: Callable[[str], Optional[float]],
    updated_at: str,
) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]], Dict[str, Any], bool]:
    overrides = dict(existing_overrides or {})
    updated_memory = dict(memory)
    metadata: Dict[str, Any] = {
        "memory_file": memory_file,
        "symbols": {},
    }
    changed = False

    write_interactive_message("")
    write_interactive_message("========== IV Rank 确认 ==========")
    write_interactive_message("请输入每个标的当前 IV Rank；如果已有本地记录，直接回车沿用。")
    write_interactive_message("如果暂时没有数据，直接回车跳过。")

    for symbol in symbols:
        symbol_key = symbol.upper()
        ticker_key = symbol_to_web_ticker(symbol)
        explicit_value = symbol_override(symbol)
        memory_record = updated_memory.get(symbol_key) or updated_memory.get(ticker_key)
        memory_value = safe_float(memory_record.get("iv_rank")) if memory_record else None

        if explicit_value is not None:
            value = explicit_value
            source = "cli_override"
            write_interactive_message(f"{symbol} IV Rank 使用命令行输入：{value:.2f}")
        else:
            value = prompt_iv_rank_value(symbol, memory_value)
            if value is None:
                source = "missing"
                write_interactive_message(f"{symbol} IV Rank 暂无记录，本次将不使用 IV Rank。")
            elif memory_value is not None and value == memory_value:
                source = "local_memory"
            else:
                source = "user_input"

        if value is not None:
            overrides[symbol_key] = value
            updated_memory[symbol_key] = {
                "iv_rank": value,
                "updated_at": updated_at,
                "source": source,
            }
            changed = True
        metadata["symbols"][symbol_key] = {
            "iv_rank": value,
            "source": source,
            "previous_memory": memory_value,
        }

    write_interactive_message("==================================")
    return overrides, updated_memory, metadata, changed
