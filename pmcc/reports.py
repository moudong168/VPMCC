import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pmcc.utils import safe_float, safe_int, safe_text


def write_report_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def format_value(value: Any, digits: int = 2, empty: str = "暂无数据") -> str:
    number = safe_float(value)
    if number is None:
        text = safe_text(value)
        return text if text is not None else empty
    return f"{number:.{digits}f}"


def html_text(value: Any) -> str:
    return html.escape(str(value if value is not None else "-"))


def html_value(value: Any, digits: int = 2, empty: str = "-") -> str:
    return html_text(format_value(value, digits, empty=empty))


def grouped_row_classes(rows: List[List[Any]], key_columns: List[int]) -> List[str]:
    classes: List[str] = []
    previous_key: Optional[Tuple[Any, ...]] = None
    group_index = -1
    for row in rows:
        key = tuple(row[index] if index < len(row) else None for index in key_columns)
        if key != previous_key:
            group_index += 1
            previous_key = key
        classes.append(f"group-row group-{group_index % 2}")
    return classes


def html_table(headers: List[str], rows: List[List[Any]], row_classes: Optional[List[str]] = None) -> str:
    if row_classes is None and rows:
        header_text = "|".join(str(header) for header in headers)
        if "Roll" in header_text or "Spot@Exp" in header_text:
            row_classes = grouped_row_classes(rows, [0, 1])
        elif any(index > 0 and row and rows[index - 1] and row[0] == rows[index - 1][0] for index, row in enumerate(rows)):
            row_classes = grouped_row_classes(rows, [0])
    head = "".join(f"<th>{html_text(header)}</th>" for header in headers)
    body_rows: List[str] = []
    for index, row in enumerate(rows):
        klass = f' class="{row_classes[index]}"' if row_classes and index < len(row_classes) and row_classes[index] else ""
        cells = "".join(f"<td>{html_text(cell)}</td>" for cell in row)
        body_rows.append(f"<tr{klass}>{cells}</tr>")
    body = "\n".join(body_rows) if body_rows else f"<tr><td colspan=\"{len(headers)}\" class=\"empty\">无</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def explain_trend(trend: Any) -> str:
    return {
        "UP": "偏强，上涨或反弹力度较明显",
        "DOWN": "偏弱，短线压力较大",
        "FLAT": "震荡，方向不明显",
        "UNKNOWN": "暂时判断不清",
    }.get(str(trend), str(trend))


def explain_action(action: Any) -> str:
    short_put_actions = {
        "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT": "short put 进入防守区：向下/向远期 roll，或准备接股",
        "PREPARE_DEFENSE": "short put 开始准备防守",
        "TAKE_PROFIT": "short put 达到止盈回补目标",
        "REVIEW": "复核 short put 风险",
    }
    if str(action) in short_put_actions:
        return short_put_actions[str(action)]
    return {
        "WAIT": "先观望，不急着新增卖出",
        "CONSIDER_SELL": "可以考虑卖出，但仓位和价格要保守",
        "SELL_CALL": "条件较适合卖出 short call",
        "SELL_CALL_WEAK": "可以偏保守地卖出 short call",
        "AVOID_SELL": "暂不建议新增卖出 short call",
        "MONITOR": "继续观察",
        "HOLD_DECAY": "继续持有，让时间价值自然衰减",
        "PLAN_ROLL": "开始规划移仓，不一定马上操作",
        "PREPARE_ROLL": "可以提前准备移仓方案",
        "ROLL_UP_OUT": "更适合向上、向远期移仓",
        "ROLL_NOW": "建议尽快处理移仓",
        "TAKE_PROFIT_AND_RESELL": "可考虑先止盈，再重新卖出",
        "DEFEND": "进入防守状态，需要重点盯盘",
        "REVIEW_EXPIRY": "接近到期，需要复核是否移仓或平仓",
        "ROLL_UP": "价格接近 short call 执行价，考虑向上移仓",
    }.get(str(action), str(action))


def explain_reason(reason: Any) -> str:
    text = str(reason)
    exact_map = {
        "Candidate structure is not ideal for a new PMCC short call": "候选合约的到期时间、Delta 或价外距离不够理想。",
        "IV rank unavailable": "当前没有拿到 IV Rank，卖出期权的胜率判断会打折。",
        "Trend and candidate structure are acceptable despite missing IV rank": "虽然缺少 IV Rank，但趋势和候选合约结构还可以。",
        "Trend is strong, so only consider a conservative short call without IV rank": "标的偏强，若要卖出也应选择更保守的执行价。",
        "Missing IV rank plus weak candidate structure argues for waiting": "缺少 IV Rank，同时候选合约也不够漂亮，因此更适合等待。",
        "IV rank is elevated": "IV Rank 较高，权利金相对更值得收。",
        "IV rank is low": "IV Rank 较低，卖出期权收租性价比不高。",
        "Wait for a cleaner DTE/OTM/delta candidate": "当前候选的到期天数、价外距离或 Delta 不够干净，建议等更合适的合约。",
        "IV rank is mid-range": "IV Rank 处于中间区域，优势不明显。",
        "Greek data unavailable in this SDK": "当前没有拿到希腊值数据，风险判断会更保守。",
        "Suggested strike is still out-of-the-money": "候选执行价仍在价外，暂时没有被行权的直接压力。",
        "Short-call delta unavailable": "当前 short call 没有拿到 Delta，无法精确衡量被打穿风险。",
        "Critical roll condition hit": "已经触发高风险移仓条件。",
        "Danger zone: delta or price proximity is elevated": "价格太接近执行价，或 Delta 偏高，风险正在升温。",
        "Most of the credit has been captured while the roll window is active": "大部分权利金已经赚到，且进入适合处理的时间窗口。",
        "Most of the credit has been captured and IV/risk conditions support recycling the short call": "大部分权利金已经赚到，可以考虑回补后重新卖出。",
        "Profit target reached inside the normal roll window": "利润目标已达到，并且处在常规移仓窗口内。",
        "Profit target reached and high IV supports considering an earlier roll": "利润目标已达到，高 IV 也支持提前考虑移仓。",
        "Roll window is active, start planning the next cycle": "已经进入移仓观察窗口，可以开始准备下一轮。",
        "Expiry is inside the 14-21 DTE attention zone; review the roll plan": "距离到期约 14-21 天，适合复核是否要移仓。",
        "Short call is still comfortably OTM": "short call 仍明显价外，可以继续吃时间价值。",
        "High IV supports rolling up and out more aggressively": "IV 较高，向上、向远期移仓时更容易收到较好的权利金。",
        "Protect LEAPS intrinsic exposure; do not let the short call stay deep ITM": "需要保护 LEAPS 的内在价值，不要让 short call 长时间深度价内。",
        "No additional short call capacity: current short calls already match LEAPS coverage.": "当前 short call 张数已经覆盖满了底仓，不应再新增卖出。",
    }
    if text in exact_map:
        return exact_map[text]
    match = re.match(r"Selected delta ([\d.\-]+)", text)
    if match:
        return f"候选合约 Delta 约为 {match.group(1)}，数值越高，价格靠近执行价时风险越大。"
    match = re.match(r"Selected expiry (\d+) DTE", text)
    if match:
        return f"候选合约距离到期约 {match.group(1)} 天。"
    match = re.match(r"Strike is ([\d.\-]+)% from spot", text)
    if match:
        return f"候选执行价距离当前股价约 {match.group(1)}%。"
    match = re.match(r"Stock is ([\d.\-]+)% away from short strike", text)
    if match:
        return f"当前股价距离 short call 执行价约 {match.group(1)}%。正数越大越安全，接近 0 或负数要更小心。"
    match = re.match(r"(\d+) days to expiry", text)
    if match:
        return f"距离到期还有 {match.group(1)} 天。"
    match = re.match(r"Short-call delta ([\d.\-]+)", text)
    if match:
        return f"当前 short call Delta 约为 {match.group(1)}，越接近 0.50 或更高，越需要防守。"
    match = re.match(r"Profit captured ([\d.\-]+)%", text)
    if match:
        return f"已赚回约 {match.group(1)}% 的权利金，可以评估是否止盈或移仓。"
    match = re.match(r"IV rank ([\d.\-]+)", text)
    if match:
        return f"IV Rank 约为 {match.group(1)}，越高通常越适合卖出收权利金。"
    match = re.match(r"Candidate short strike ([\d.\-]+) is below McMillan cost-line guard ([\d.\-]+).", text)
    if match:
        return f"候选执行价 {match.group(1)} 低于成本线保护价 {match.group(2)}，不适合为了收租牺牲 LEAPS 保护。"
    match = re.match(r"(.+): long-leg delta ([\d.\-]+) is below", text)
    if match:
        return f"{match.group(1)} 的 LEAPS Delta 约 {match.group(2)}，作为 PMCC 底仓偏弱。"
    match = re.match(r"(.+): extrinsic value is ([\d.\-]+)%", text)
    if match:
        return f"{match.group(1)} 的外在价值占比较高，底仓自身时间损耗更明显。"
    match = re.match(r"(.+): long leg has only (\d+) DTE", text)
    if match:
        return f"{match.group(1)} 距离到期只剩 {match.group(2)} 天，作为 LEAPS 底仓偏短。"
    return text


def clean_sentence_fragment(text: str) -> str:
    return text.rstrip("。；;,.， ")


def risk_rank(label: str) -> int:
    return {"RED": 4, "ORANGE": 3, "YELLOW": 2, "GREEN": 1, "GRAY": 0}.get(label, 0)


def short_call_risk_light(review: Dict[str, Any]) -> Tuple[str, str]:
    action = str(review.get("roll_action") or "")
    delta = safe_float(review.get("delta"))
    dte = safe_int(review.get("days_to_expiry"))
    price_gap = safe_float(review.get("price_to_strike_pct"))

    if action == "ROLL_NOW" or (delta is not None and delta >= 0.65) or (price_gap is not None and price_gap < 0):
        return "RED", "act now"
    if action in {"ROLL_UP_OUT", "DEFEND"} or (delta is not None and delta >= 0.50) or (dte is not None and dte <= 7):
        return "ORANGE", "defend"
    if action in {"PLAN_ROLL", "PREPARE_ROLL", "ROLL_UP"} or (delta is not None and delta >= 0.40) or (price_gap is not None and price_gap <= 3):
        return "YELLOW", "plan/watch"
    if action in {"HOLD_DECAY", "MONITOR"}:
        return "GREEN", "hold"
    return "GRAY", "unknown"


def short_put_risk_light(review: Dict[str, Any]) -> Tuple[str, str]:
    action = str(review.get("roll_action") or "")
    delta = safe_float(review.get("delta"))
    abs_delta = abs(delta) if delta is not None else None
    dte = safe_int(review.get("days_to_expiry"))
    otm_pct = safe_float(review.get("underlying_to_strike_pct"))

    if action == "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT" or (otm_pct is not None and otm_pct <= 0) or (abs_delta is not None and abs_delta >= 0.50):
        return "RED", "put defend"
    if action == "PREPARE_DEFENSE" or (abs_delta is not None and abs_delta >= 0.30) or (dte is not None and dte <= 7):
        return "ORANGE", "put defense prep"
    if action == "REVIEW" or (abs_delta is not None and abs_delta >= 0.20) or (otm_pct is not None and otm_pct <= 10):
        return "YELLOW", "put review"
    if action in {"MONITOR", "TAKE_PROFIT"}:
        return "GREEN", "hold"
    return "GRAY", "unknown"


def symbol_risk_light(recommendation: Dict[str, Any]) -> Tuple[str, str]:
    if (recommendation.get("short_call_reviews") or []) or (recommendation.get("short_put_reviews") or []):
        ranked = []
        for review in recommendation.get("short_call_reviews") or []:
            ranked.append(short_call_risk_light(review))
        for review in recommendation.get("short_put_reviews") or []:
            ranked.append(short_put_risk_light(review))
        ranked = sorted(ranked, key=lambda item: risk_rank(item[0]), reverse=True)
        return ranked[0]

    action = str(recommendation.get("action") or "")
    if action in {"SELL_CALL", "SELL_CALL_WEAK"}:
        return "GREEN", "sell candidate"
    if action == "CONSIDER_SELL":
        return "YELLOW", "small/conservative"
    if action in {"WAIT", "AVOID_SELL", "MONITOR"}:
        return "GRAY", "no new short call"
    return "GRAY", "unknown"


def risk_class(light: str) -> str:
    if light == "RED":
        return "risk-red"
    if light == "ORANGE":
        return "risk-orange"
    if light == "YELLOW":
        return "risk-yellow"
    if light == "GREEN":
        return "risk-green"
    if light == "GRAY":
        return "risk-gray"
    if "红" in light:
        return "risk-red"
    if "橙" in light:
        return "risk-orange"
    if "黄" in light:
        return "risk-yellow"
    if "绿" in light:
        return "risk-green"
    return "risk-gray"


def event_risk_class(event_block: Dict[str, Any]) -> str:
    if not event_block:
        return "event-normal"
    if event_block.get("blocked"):
        return "event-blocked"
    if event_block.get("blocking_events"):
        return "event-blocked"
    if event_block.get("attention_events"):
        return "event-attention"
    return "event-normal"


def validation_risk_class(validation: Dict[str, Any]) -> str:
    earnings = validation.get("earnings_check") or {}
    status = str(earnings.get("status") or "").upper()
    if status == "WARN":
        return "validation-warn"
    if status == "ATTENTION":
        return "validation-attention"
    if status == "UNAVAILABLE":
        return "validation-unavailable"
    return "validation-ok"


def summarize_data_validation(validation: Dict[str, Any]) -> str:
    if not validation or not validation.get("enabled"):
        return "web validation disabled"
    price = validation.get("price_check") or {}
    earnings = validation.get("earnings_check") or {}
    parts = [f"price {price.get('status', 'SKIPPED')}"]
    if price.get("yahoo_price") is not None:
        parts.append(
            f"Yahoo {format_value(price.get('yahoo_price'), 2)}, diff {format_value(price.get('percent_diff'), 2)}%"
        )
    parts.append(f"earnings {earnings.get('status', 'SKIPPED')}")
    if earnings.get("next_earnings_date"):
        parts.append(f"{earnings.get('next_earnings_date')} ({earnings.get('days_to_earnings')}d)")
    return "; ".join(parts)


def summarize_iv_rank_analysis(analysis: Dict[str, Any]) -> str:
    if not analysis:
        return "IV Rank source unavailable"
    if analysis.get("is_true_historical_iv_rank") or analysis.get("is_decision_usable_iv_rank"):
        source = analysis.get("source") or "historical IV"
        count = analysis.get("lookback_count")
        count_text = f", {count} points" if count else ""
        if analysis.get("priority") == "fresh_manual_memory_fallback":
            updated_at = analysis.get("memory_updated_at")
            age = analysis.get("memory_age_days")
            age_text = f", age {format_value(age, 2)}d" if age is not None else ""
            return f"same-day manual IV Rank fallback from {updated_at or source}{age_text}"
        proxy_meta = analysis.get("chain_iv_rank_proxy_meta") or {}
        proxy_status = proxy_meta.get("status")
        proxy_text = f"; chain proxy {proxy_status} (not used)" if proxy_status else ""
        return f"historical IV Rank from {source}{count_text}{proxy_text}"
    proxy = analysis.get("chain_iv_rank_proxy")
    proxy_meta = analysis.get("chain_iv_rank_proxy_meta") or {}
    proxy_status = proxy_meta.get("status")
    sample = proxy_meta.get("contracts_after_outlier_filter")
    status = analysis.get("history_status") or "unavailable"
    if proxy is not None:
        sample_text = f", n={sample}" if sample is not None else ""
        return f"historical IV Rank unavailable ({status}); chain proxy {format_value(proxy, 1)} {proxy_status or ''}{sample_text} excluded from decision"
    proxy_text = f"; chain proxy {proxy_status} excluded from decision" if proxy_status else ""
    return f"historical IV Rank unavailable ({status}){proxy_text}"


def summarize_iv_environment(environment: Dict[str, Any]) -> str:
    if not environment:
        return "IV environment unavailable"
    parts = [str(environment.get("label") or "UNKNOWN")]
    if environment.get("iv_hv_ratio") is not None:
        parts.append(f"IV/HV {format_value(environment.get('iv_hv_ratio'), 2)}")
    if environment.get("candidate_short_call_iv") is not None:
        parts.append(f"short IV {format_value(environment.get('candidate_short_call_iv'), 2)}")
    if environment.get("average_long_leg_iv") is not None:
        parts.append(f"long IV {format_value(environment.get('average_long_leg_iv'), 2)}")
    if environment.get("short_vs_long_iv_spread") is not None:
        parts.append(f"short-long {format_value(environment.get('short_vs_long_iv_spread'), 2)}")
    if environment.get("average_existing_slot_short_long_iv_spread") is not None:
        parts.append(f"existing slot avg {format_value(environment.get('average_existing_slot_short_long_iv_spread'), 2)}")
    if environment.get("average_candidate_slot_short_long_iv_spread") is not None:
        parts.append(f"candidate slot avg {format_value(environment.get('average_candidate_slot_short_long_iv_spread'), 2)}")
    return "; ".join(parts)


def summarize_event_block(event_block: Dict[str, Any]) -> str:
    if not event_block:
        return "event block unavailable"
    blocking = event_block.get("blocking_events") or []
    if event_block.get("blocked") and blocking:
        first = blocking[0]
        source = first.get("source_name") or first.get("source")
        source_text = f" from {source}" if source else ""
        date_text = f" on {first.get('date')}" if first.get("date") else ""
        return f"BLOCKED: {first.get('type')}{date_text} in {first.get('days_to_event')}d{source_text}; no new short calls"
    attention = event_block.get("attention_events") or []
    if attention:
        first = attention[0]
        return f"{first.get('type')} in {first.get('days_to_event')}d; size conservatively"
    return "no near-term event block"


def format_terminal_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    text_rows = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(str(header)), *(len(row[index]) for row in text_rows)) if text_rows else len(str(header))
        for index, header in enumerate(headers)
    ]
    header_line = "  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers))
    divider = "  ".join("-" * width for width in widths)
    lines = [header_line, divider]
    lines.extend("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))) for row in text_rows)
    return lines


def format_net_roll_price(candidate: Dict[str, Any]) -> str:
    if candidate.get("estimated_net_debit") is not None:
        return f"-{format_value(candidate.get('estimated_net_debit'), 2)}"
    if candidate.get("estimated_net_credit") is not None:
        return f"+{format_value(candidate.get('estimated_net_credit'), 2)}"
    return "-"


def format_roll_pnl_value(candidate: Dict[str, Any], field: str) -> str:
    pnl = candidate.get("whole_symbol_roll_pnl") or {}
    value = pnl.get(field)
    if value is None:
        missing = pnl.get("missing_pnl_inputs") or []
        return "needs price" if missing else "-"
    return format_value(value, 2)


def html_roll_pnl_detail_section(recommendation: Dict[str, Any]) -> str:
    component_rows: List[List[Any]] = []
    scenario_rows: List[List[Any]] = []
    for review in recommendation.get("short_call_reviews") or []:
        for candidate in (review.get("roll_candidates") or [])[:3]:
            pnl = candidate.get("whole_symbol_roll_pnl") or {}
            if not pnl:
                continue
            missing = pnl.get("missing_pnl_inputs") or []
            warning = pnl.get("pnl_warning") or ("; ".join(missing) if missing else "-")
            component_rows.append(
                [
                    review.get("code") or "-",
                    candidate.get("code") or "-",
                    format_roll_pnl_value(candidate, "roll_net_cashflow_est"),
                    format_roll_pnl_value(candidate, "old_short_realized_pnl_est"),
                    format_roll_pnl_value(candidate, "long_leg_unrealized_pnl_est"),
                    format_roll_pnl_value(candidate, "symbol_total_pnl_before_roll_est"),
                    format_roll_pnl_value(candidate, "symbol_total_pnl_after_roll_est"),
                    warning,
                ]
            )
            for scenario in (pnl.get("after_roll_scenarios") or [])[:5]:
                scenario_rows.append(
                    [
                        candidate.get("code") or "-",
                        format_value(scenario.get("spot"), 2),
                        format_value(scenario.get("old_short_realized_pnl_est"), 2),
                        format_value(scenario.get("long_intrinsic_pnl_est"), 2),
                        format_value(scenario.get("new_short_pnl_at_expiry_est"), 2),
                        format_value(scenario.get("symbol_total_pnl_est"), 2),
                    ]
                )
    if not component_rows:
        return ""
    scenario_table = ""
    if scenario_rows:
        scenario_table = html_table(
            ["Roll到", "股价情景", "旧Short P/L", "Long内在P/L", "新Short到期P/L", "整体P/L"],
            scenario_rows,
        )
    return f"""
              <h3>Roll 整体标的 P/L 估算</h3>
              {html_table(["原Short", "Roll到", "Roll现金流", "旧Short P/L", "Long P/L", "Roll前整体P/L", "Roll后整体P/L", "提示"], component_rows)}
              {scenario_table}
              <p class="muted">以上为同平台、同账户、同标的估算；情景表为静态内在价值口径，不等同于真实成交或未来 Greeks 估值。</p>
"""


def effective_candidate_capacity(candidate: Dict[str, Any], coverage: Dict[str, Any]) -> Optional[int]:
    candidate_capacity = safe_int(candidate.get("max_new_short_calls"))
    if candidate_capacity is not None:
        return candidate_capacity
    return safe_int(coverage.get("available_to_sell"))


def format_liquidity_summary(candidate: Dict[str, Any]) -> str:
    liquidity = candidate.get("liquidity") or {}
    if not liquidity:
        return "-"
    status = "PASS" if liquidity.get("ok") else "FAIL"
    parts = [
        status,
        f"spr {format_value(liquidity.get('spread_pct'), 1)}%",
        f"OI {format_value(liquidity.get('open_interest'), 0)}",
        f"vol {format_value(liquidity.get('volume'), 0)}",
        f"last/mid {format_value(liquidity.get('last_mid_deviation_pct'), 1)}%",
    ]
    reasons = liquidity.get("reasons") or []
    if reasons:
        parts.append("; ".join(reasons[:2]))
    return " | ".join(parts)


def format_leaps_slot_rows(slot_analysis: Dict[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for slot in (slot_analysis.get("slots") or []):
        if slot.get("occupied_by"):
            status = f"已占用：{slot.get('occupied_by')}"
        elif slot.get("eligible_for_candidate"):
            status = "候选可覆盖"
        else:
            status = "候选不可覆盖"
        rows.append(
            [
                slot.get("leaps_code") or f"槽位 {slot.get('slot')}",
                format_value(slot.get("leaps_strike"), 2),
                slot.get("leaps_expiry") or "-",
                format_value(slot.get("leaps_cost"), 2),
                format_value(slot.get("leaps_iv"), 2, empty="-"),
                format_value(slot.get("minimum_safe_short_strike"), 2),
                slot.get("paired_short_code") or "-",
                format_value(slot.get("paired_short_iv"), 2, empty="-"),
                format_value(slot.get("short_long_iv_spread"), 2, empty="-"),
                status,
            ]
        )
    return rows


def format_leaps_slot_summary(slot_analysis: Dict[str, Any]) -> str:
    if not slot_analysis:
        return "No per-LEAPS slot data."
    total = safe_int(slot_analysis.get("total_slots")) or 0
    occupied = safe_int(slot_analysis.get("occupied_slots")) or 0
    eligible = safe_int(slot_analysis.get("eligible_new_short_call_slots")) or 0
    candidate_strike = format_value(slot_analysis.get("candidate_strike"), 2, empty="-")
    min_safe = format_value(slot_analysis.get("minimum_candidate_safe_strike"), 2, empty="-")
    max_safe = format_value(slot_analysis.get("max_unoccupied_safe_strike"), 2, empty="-")
    parts = [
        f"Per-LEAPS slots: total {total}, occupied {occupied}; candidate strike {candidate_strike} can cover {eligible} open slot(s); unoccupied safe-strike min/max {min_safe}/{max_safe}."
    ]
    existing_spread = slot_analysis.get("average_existing_short_long_iv_spread")
    candidate_spread = slot_analysis.get("average_candidate_short_long_iv_spread")
    if existing_spread is not None:
        parts.append(f"Existing paired short-long IV avg {format_value(existing_spread, 2)}.")
    if candidate_spread is not None:
        parts.append(f"Candidate short-long IV avg {format_value(candidate_spread, 2)}.")
    return " ".join(parts)


def html_leaps_slot_table(slot_analysis: Dict[str, Any]) -> str:
    rows = format_leaps_slot_rows(slot_analysis)
    if not rows:
        return "<p class=\"muted\">No per-LEAPS slot data.</p>"
    return html_table(
        [
            "LEAPS CALL",
            "LEAPS strike",
            "LEAPS expiry",
            "LEAPS cost",
            "LEAPS IV",
            "minimum safe short strike",
            "paired/candidate short",
            "short IV",
            "short-long IV",
            "status",
        ],
        rows,
    )


def format_long_leg_expiry_summary(long_leg_analysis: Dict[str, Any]) -> str:
    if not long_leg_analysis:
        return "Long-leg expiry risk: unavailable."
    buckets = long_leg_analysis.get("expiry_buckets") or []
    bucket_text = f"{len(buckets)} expiry bucket" + ("" if len(buckets) == 1 else "s")
    return (
        "Long-leg expiry risk: "
        f"{bucket_text}; "
        f"net long Delta {format_value(long_leg_analysis.get('net_long_delta'), 3)}, "
        f"avg Delta {format_value(long_leg_analysis.get('average_delta'), 3)}, "
        f"time-weighted Delta {format_value(long_leg_analysis.get('time_weighted_delta'), 3)}, "
        f"short-DTE-risk-weighted Delta {format_value(long_leg_analysis.get('dte_risk_weighted_delta'), 3)}, "
        f"min DTE {format_value(long_leg_analysis.get('min_long_dte'), 0)}, "
        f"min leg Delta {format_value(long_leg_analysis.get('min_long_delta'), 3)}, "
        f"max leg Gamma {format_value(long_leg_analysis.get('max_long_gamma'), 5)}."
    )


def format_long_leg_expiry_rows(long_leg_analysis: Dict[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for bucket in long_leg_analysis.get("expiry_buckets") or []:
        rows.append(
            [
                bucket.get("expiry") or "-",
                bucket.get("quantity") or 0,
                format_value(bucket.get("min_dte"), 0),
                format_value(bucket.get("max_dte"), 0),
                format_value(bucket.get("average_delta"), 3),
                format_value(bucket.get("net_delta"), 3),
                format_value(bucket.get("net_gamma"), 5),
                format_value(bucket.get("net_vega"), 3),
            ]
        )
    return rows


def html_long_leg_expiry_table(long_leg_analysis: Dict[str, Any]) -> str:
    rows = format_long_leg_expiry_rows(long_leg_analysis)
    if not rows:
        return "<p class=\"muted\">No long-leg expiry bucket data.</p>"
    return html_table(["Expiry", "Qty", "Min DTE", "Max DTE", "Avg Delta", "Net Delta", "Net Gamma", "Net Vega"], rows)


def format_candidate_for_human(candidate: Dict[str, Any], available_to_sell: Optional[int] = None) -> List[str]:
    if not candidate or not candidate.get("code"):
        return ["候选合约：本次没有找到足够清晰的新增卖出候选。"]

    consider = candidate.get("consider_selling")
    prefix = "候选合约："
    if consider is False:
        prefix = "候选合约仅供参考："

    line = (
        f"{prefix}{candidate.get('code')}，执行价 {format_value(candidate.get('strike'))}，"
        f"到期 {candidate.get('expiry') or '暂无数据'}，约 {format_value(candidate.get('days_to_expiry'), 0)} 天，"
        f"Delta {format_value(candidate.get('delta'), 3)}。"
    )
    lines = [line]
    candidate_capacity = safe_int(candidate.get("max_new_short_calls"))
    display_capacity = candidate_capacity if candidate_capacity is not None else available_to_sell
    if display_capacity is not None:
        capacity_note = "逐张 LEAPS 槽位保护后" if candidate_capacity is not None else "只按同一标的 LEAPS 底仓计算"
        lines.append(f"可新增张数：最多 {display_capacity} 张（{capacity_note}）。")
    if candidate.get("mcmillan_safety") in {"BLOCKED_BELOW_COST_LINE", "BLOCKED_NO_ELIGIBLE_LEAPS_SLOT"}:
        lines.append("保护提醒：候选执行价没有足够的逐张 LEAPS 成本线覆盖槽位，程序已把新增卖出倾向调为保守。")
    liquidity = candidate.get("liquidity") or {}
    if liquidity and not liquidity.get("ok"):
        lines.append("流动性提醒：" + "；".join((liquidity.get("reasons") or [])[:3]) + "。")
    event_block = candidate.get("event_block") or {}
    if event_block.get("blocked"):
        lines.append("事件提醒：" + summarize_event_block(event_block) + "。")
    safe_alternative = candidate.get("mcmillan_safe_alternative")
    if safe_alternative and safe_alternative.get("code"):
        lines.append(
            f"更保守替代：{safe_alternative.get('code')}，执行价 {format_value(safe_alternative.get('strike'))}，"
            f"约 {format_value(safe_alternative.get('days_to_expiry'), 0)} 天，"
            f"Delta {format_value(safe_alternative.get('delta'), 3)}。"
        )
    return lines


def build_short_put_operation_advice(review: Dict[str, Any]) -> str:
    code = review.get("code") or "short put"
    wheel_state = review.get("wheel_state") or {}
    payoff = review.get("payoff_scenarios") or {}
    action = str(review.get("roll_action") or "")
    state = str(wheel_state.get("state") or "")
    payoff_action = str(payoff.get("operation_advice") or "")
    delta = safe_float(review.get("delta"))
    abs_delta = safe_float(review.get("abs_delta"))
    if abs_delta is None and delta is not None:
        abs_delta = abs(delta)
    dte = safe_int(review.get("days_to_expiry"))
    otm_pct = safe_float(review.get("underlying_to_strike_pct"))
    close_target = review.get("close_target_50pct")

    if action == "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT" or state == "CSP_DEFEND_OR_ACCEPT_ASSIGNMENT":
        return f"{code} 操作建议：进入防守区，优先比较接股和向下/向远期 roll；不建议继续被动等待。"
    if action == "PREPARE_DEFENSE" or state == "CSP_DEFENSE_PREP":
        return f"{code} 操作建议：暂不急着 roll，但要准备防守单；若 Put |Delta| 接近 0.50 或股价继续靠近行权价，再执行 roll。"
    if action == "TAKE_PROFIT" or state in {"CSP_PROFIT_TAKE", "CSP_PROFIT_TAKE_STRONG"}:
        return f"{code} 操作建议：已接近止盈区，优先考虑回补释放风险；若重新卖 put，需要重新比较 IV 和下方支撑。"

    watch = []
    if dte is not None and dte > 21:
        watch.append("等进入 21 DTE 管理窗口再提高检查频率")
    if abs_delta is not None:
        if abs_delta >= 0.30:
            watch.append(f"Put |Delta| {format_value(abs_delta, 3)} 已进入 0.30+ 防守准备区")
        elif abs_delta >= 0.20:
            watch.append(f"Put |Delta| {format_value(abs_delta, 3)} 已进入 0.20-0.30 观察区间")
        else:
            watch.append("若 Put |Delta| 升到 0.20-0.30 以上再复核")
    if otm_pct is not None:
        watch.append("若距行权价收窄到 10% 以内再准备防守")
    if close_target is not None:
        watch.append(f"50% 回补目标约 {format_value(close_target, 2)}")
    if payoff_action == "HOLD_WITH_20PCT_STRESS_AWARENESS":
        watch.append("20% 压力情景会进入亏损区，注意仓位现金占用")

    suffix = "；".join(watch[:4])
    if suffix:
        return f"{code} 操作建议：继续持有，不急着 roll。关注：{suffix}。"
    return f"{code} 操作建议：继续持有，不急着 roll。"


def build_chinese_summary(result: Dict[str, Any]) -> str:
    if "error" in result:
        return f"{result.get('symbol', '标的')} 分析失败：{result['error']}"

    option = result.get("suggested_option", {})
    action_text_map = {
        "WAIT": "暂时观望",
        "CONSIDER_SELL": "可考虑卖出 short call",
        "SELL_CALL": "适合卖出 short call",
        "SELL_CALL_WEAK": "适合偏保守地卖出 short call",
        "AVOID_SELL": "暂不建议卖出 short call",
    }
    parts = [
        f"{result['symbol']} 当前价格约为 {result.get('price')}",
        f"趋势判断为 {result.get('trend')}",
        f"主建议是：{action_text_map.get(result.get('action'), result.get('action'))}",
    ]
    if option.get("code"):
        parts.append(f"候选 short call 为 {option['code']}，执行价 {option.get('strike')}，距离到期约 {option.get('days_to_expiry')} 天")
    reviews = result.get("short_call_reviews", [])
    if reviews:
        roll_action_text_map = {
            "MONITOR": "继续观察",
            "HOLD_DECAY": "继续吃时间价值",
            "PLAN_ROLL": "开始规划 roll",
            "PREPARE_ROLL": "可提前准备 roll",
            "ROLL_UP_OUT": "适合向上并向外 roll",
            "ROLL_NOW": "建议尽快 roll",
            "TAKE_PROFIT_AND_RESELL": "可止盈后重新卖出",
            "DEFEND": "进入防守区，需要重点盯盘",
            "REVIEW_EXPIRY": "接近到期，需复核是否移仓",
        }
        review_text = "；".join(
            f"{item['code']}：{roll_action_text_map.get(item['roll_action'], item['roll_action'])}"
            for item in reviews
        )
        parts.append(f"现有 short call 处理建议：{review_text}")
    return "。".join(parts) + "。"


def build_chinese_summary_clean(result: Dict[str, Any]) -> str:
    if "error" in result:
        return f"{result.get('symbol', '标的')} 分析失败：{result['error']}"

    option = result.get("candidate_short_call", result.get("suggested_option", {}))
    action_text_map = {
        "WAIT": "暂时观望",
        "CONSIDER_SELL": "可考虑卖出 short call",
        "SELL_CALL": "适合卖出 short call",
        "SELL_CALL_WEAK": "适合偏保守地卖出 short call",
        "AVOID_SELL": "暂不建议卖出 short call",
    }
    parts = [
        f"{result['symbol']} 当前价格约为 {result.get('price')}",
        f"趋势判断为 {explain_trend(result.get('trend'))}",
        f"主建议是：{action_text_map.get(result.get('action'), result.get('action'))}",
    ]

    if option.get("code"):
        parts.append(
            f"新增卖出候选 short call 为 {option['code']}，执行价 {option.get('strike')}，距离到期约 {option.get('days_to_expiry')} 天"
        )
        if option.get("mcmillan_safety") in {"BLOCKED_BELOW_COST_LINE", "BLOCKED_NO_ELIGIBLE_LEAPS_SLOT"}:
            parts.append("但该候选执行价低于成本线保护价，暂不适合直接卖出")
        safe_alternative = option.get("mcmillan_safe_alternative")
        if safe_alternative and safe_alternative.get("code"):
            parts.append(
                f"更保守的保护线替代合约是 {safe_alternative['code']}，执行价 {safe_alternative.get('strike')}"
            )

    reviews = result.get("short_call_reviews", [])
    if reviews:
        roll_action_text_map = {
            "MONITOR": "继续观察",
            "HOLD_DECAY": "继续吃时间价值",
            "PLAN_ROLL": "开始规划 roll",
            "PREPARE_ROLL": "可提前准备 roll",
            "ROLL_UP_OUT": "适合向上并向外 roll",
            "ROLL_NOW": "建议尽快 roll",
            "TAKE_PROFIT_AND_RESELL": "可止盈后重新卖出",
            "DEFEND": "进入防守区，需要重点防守",
            "REVIEW_EXPIRY": "接近到期，需复核是否移仓",
        }
        review_text = "；".join(
            f"{item['code']}：{roll_action_text_map.get(item['roll_action'], item['roll_action'])}"
            for item in reviews
        )
        parts.append(f"现有 short call 处理建议：{review_text}")

    return "。".join(parts) + "。"


def format_symbol_human(symbol_result: Dict[str, Any], position_result: Optional[Dict[str, Any]] = None) -> List[str]:
    position_result = position_result or {}
    symbol = symbol_result.get("symbol") or position_result.get("symbol") or "标的"
    if "error" in symbol_result or "error" in position_result:
        error = symbol_result.get("error") or position_result.get("error")
        return [f"【{symbol}】分析失败：{error}", "请先确认 Futu OpenD 已启动、已登录，并且美股行情和交易权限正常。"]

    coverage = position_result.get("coverage") or {}
    candidate = symbol_result.get("candidate_short_call") or symbol_result.get("suggested_option") or {}
    available = safe_int(coverage.get("available_to_sell"))
    lines = [
        f"【{symbol}】",
        f"当前价格：{format_value(position_result.get('price', symbol_result.get('price')))}；趋势：{explain_trend(position_result.get('trend', symbol_result.get('trend')))}；IV Rank：{format_value(position_result.get('iv_rank_est', symbol_result.get('iv_rank_est')))}。",
    ]
    iv_rank_analysis = position_result.get("iv_rank_analysis") or symbol_result.get("iv_rank_analysis") or {}
    if iv_rank_analysis:
        lines.append(f"IV Rank 来源：{summarize_iv_rank_analysis(iv_rank_analysis)}。")
    iv_environment = position_result.get("iv_environment") or symbol_result.get("iv_environment") or {}
    if iv_environment:
        lines.append(f"IV 环境：{summarize_iv_environment(iv_environment)}。")
    data_quality = position_result.get("data_quality") or symbol_result.get("data_quality") or {}
    if data_quality:
        quality_parts = [
            f"status {data_quality.get('status')}",
            f"Delta {data_quality.get('delta_values', 0)}/{data_quality.get('contracts_checked', 0)}",
            f"IV {data_quality.get('iv_values', 0)}/{data_quality.get('contracts_checked', 0)}",
        ]
        if data_quality.get("issues"):
            quality_parts.append("issues: " + "; ".join(data_quality.get("issues", [])[:3]))
        elif data_quality.get("warnings"):
            quality_parts.append("warnings: " + "; ".join(data_quality.get("warnings", [])[:3]))
        lines.append("Futu option data quality: " + "; ".join(quality_parts) + ".")
    validation = position_result.get("data_validation") or symbol_result.get("data_validation") or {}
    if validation:
        lines.append(f"备用数据源校验：{summarize_data_validation(validation)}。")
    event_block = position_result.get("event_block") or symbol_result.get("event_block") or {}
    if event_block:
        lines.append(f"事件窗口检查：{summarize_event_block(event_block)}。")
    long_leg_analysis = position_result.get("long_leg_analysis") or {}
    if long_leg_analysis:
        lines.append(format_long_leg_expiry_summary(long_leg_analysis))
        expiry_rows = format_long_leg_expiry_rows(long_leg_analysis)
        if len(expiry_rows) > 1:
            lines.append("Long-leg expiry buckets:")
            for expiry, quantity, min_dte, max_dte, avg_delta, net_delta, net_gamma, net_vega in expiry_rows:
                lines.append(
                    f"- {expiry}: qty {quantity}, DTE {min_dte}-{max_dte}, "
                    f"avg Delta {avg_delta}, net Delta {net_delta}, net Gamma {net_gamma}, net Vega {net_vega}."
                )
    if coverage:
        lines.append(
            f"仓位覆盖：LEAPS/底仓 {format_value(coverage.get('base_contracts'), 0)} 张，"
            f"已卖 short call {format_value(coverage.get('short_call_contracts'), 0)} 张，"
            f"还可新增 {format_value(coverage.get('available_to_sell'), 0)} 张。"
        )
    slot_analysis = candidate.get("leaps_coverage_slot_analysis") or position_result.get("leaps_coverage_slots") or {}
    if slot_analysis:
        lines.append(format_leaps_slot_summary(slot_analysis))
        slot_rows = format_leaps_slot_rows(slot_analysis)
        if slot_rows:
            lines.append("逐张槽位明细：")
            for row in slot_rows:
                leaps_code, leaps_strike, leaps_expiry, leaps_cost, leaps_iv, safe_strike, paired_short, short_iv, iv_spread, status = row
                iv_detail = f"LEAPS IV {leaps_iv}; short {paired_short}; short IV {short_iv}; short-long IV {iv_spread}; "
                lines.append(
                    f"- {leaps_code}: {iv_detail}LEAPS strike {leaps_strike}; expiry {leaps_expiry}; cost {leaps_cost}; "
                    f"minimum safe short strike {safe_strike}; {status}."
                )
                continue
                lines.append(
                    f"- {leaps_code}: LEAPS strike {leaps_strike}，到期 {leaps_expiry}，成本 {leaps_cost}；"
                    f"最低安全 short strike {safe_strike}；{status}。"
                )

    lines.append(f"主结论：{explain_action(symbol_result.get('action'))}。")

    reasons = [clean_sentence_fragment(explain_reason(item)) for item in symbol_result.get("reason", [])]
    if reasons:
        lines.append("主要原因：" + "；".join(reasons[:5]) + "。")

    lines.extend(format_candidate_for_human(candidate, available))

    reviews = symbol_result.get("short_call_reviews", [])
    if reviews:
        lines.append("现有 short call：")
        for item in reviews:
            lines.append(
                f"- {item.get('code')}：{explain_action(item.get('roll_action'))}。"
                f"执行价 {format_value(item.get('strike'))}，到期约 {format_value(item.get('days_to_expiry'), 0)} 天，"
                f"Delta {format_value(item.get('delta'), 3)}。"
            )
            item_reasons = [clean_sentence_fragment(explain_reason(reason)) for reason in item.get("reason", [])]
            if item_reasons:
                lines.append(f"  理由：{'；'.join(item_reasons[:4])}。")
            roll_candidates = item.get("roll_candidates") or []
            if roll_candidates:
                lines.append("  ROLL 候选：")
                for candidate in roll_candidates[:3]:
                    net_text = "净收支暂无数据"
                    if candidate.get("estimated_net_debit") is not None:
                        net_text = f"估算净支出 {format_value(candidate.get('estimated_net_debit'), 2)}"
                    elif candidate.get("estimated_net_credit") is not None:
                        net_text = f"估算净收入 {format_value(candidate.get('estimated_net_credit'), 2)}"
                    lines.append(
                        f"  - {candidate.get('code')}，执行价 {format_value(candidate.get('strike'))}，"
                        f"到期约 {format_value(candidate.get('days_to_expiry'), 0)} 天，"
                        f"Delta {format_value(candidate.get('delta'), 3)}，"
                        f"比原执行价上移 {format_value(candidate.get('strike_lift'))}，{net_text}。"
                    )

    put_reviews = symbol_result.get("short_put_reviews", [])
    if put_reviews:
        lines.append("Short put / cash-secured put:")
        for item in put_reviews:
            wheel_state = item.get("wheel_state") or {}
            lines.append(
                f"- {item.get('code')}: {explain_action(item.get('roll_action'))}. "
                f"Strike {format_value(item.get('strike'))}, DTE {format_value(item.get('days_to_expiry'), 0)}, "
                f"Delta {format_value(item.get('delta'), 3)}, OTM {format_value(item.get('underlying_to_strike_pct'), 2)}%, "
                f"mark {format_value(item.get('mark_price'), 2)}, credit {format_value(item.get('credit_received'), 2)}, "
                f"profit captured {format_value(item.get('profit_capture_pct'), 1)}%, break-even {format_value(item.get('break_even'), 2)}."
            )
            if wheel_state:
                lines.append(
                    f"  Wheel state: {wheel_state.get('state')}; action: {wheel_state.get('action')}; "
                    f"priority: {wheel_state.get('priority')}; next trigger: {wheel_state.get('next_check_trigger')}"
                )
            close_target = item.get("close_target_50pct")
            if close_target is not None:
                lines.append(f"  50% buyback target: {format_value(close_target, 2)}.")
            advice_text = item.get("operation_advice_text") or build_short_put_operation_advice(item)
            if advice_text:
                lines.append(f"  {advice_text}")
            payoff = item.get("payoff_scenarios") or {}
            if payoff.get("summary"):
                lines.append(f"  Expiration payoff: {payoff.get('summary')}")
                lines.append("  Payoff scenarios at expiry:")
                for scenario in (payoff.get("rows") or [])[:12]:
                    assigned = "assigned" if scenario.get("assigned") else "expires OTM"
                    lines.append(
                        f"  - Spot {format_value(scenario.get('spot_at_expiry'), 2)} "
                        f"({format_value(scenario.get('spot_change_pct'), 1)}%): "
                        f"P/L {format_value(scenario.get('pnl_total'), 2)}, {assigned}, "
                        f"effective cost {format_value(scenario.get('effective_share_cost'), 2, empty='-')}."
                    )
            roll_candidates = item.get("roll_candidates") or []
            if roll_candidates:
                lines.append("  Short put roll candidates:")
                for candidate in roll_candidates[:3]:
                    net_text = "net unknown"
                    if candidate.get("estimated_net_debit") is not None:
                        net_text = f"estimated net debit {format_value(candidate.get('estimated_net_debit'), 2)}"
                    elif candidate.get("estimated_net_credit") is not None:
                        net_text = f"estimated net credit {format_value(candidate.get('estimated_net_credit'), 2)}"
                    lines.append(
                        f"  - {candidate.get('code')}: strike {format_value(candidate.get('strike'))}, "
                        f"DTE {format_value(candidate.get('days_to_expiry'), 0)}, Delta {format_value(candidate.get('delta'), 3)}, "
                        f"strike change {format_value(candidate.get('strike_change'))}, {net_text}."
                    )

    summary = symbol_result.get("summary_cn")
    if summary:
        lines.append(f"一句话：{summary}")
    return lines


def format_plain_text_report(result: Dict[str, Any]) -> str:
    lines = build_trader_summary_report(result)
    lines.extend(["", "========== 白话解读 =========="])
    if result.get("mode") in {"pmcc_two_stage", "pmcc_two_stage_multi_portfolio"}:
        source = result.get("position_source", {})
        lines.append(
            f"本次从 Futu OpenD 和手动补充仓位中识别：底仓 {format_value(source.get('total_base_contracts'), 0)} 张，"
            f"已卖 short call {format_value(source.get('total_short_call_contracts'), 0)} 张。"
        )
        stage_1_by_symbol = build_stage_1_lookup(result.get("stage_1_position_analysis", []))
        for recommendation in result.get("stage_2_operation_recommendations", []):
            lines.append("")
            lines.extend(format_symbol_human(recommendation, stage_1_by_symbol.get(analysis_lookup_key(recommendation))))
    else:
        lines.extend(format_symbol_human(result))

    lines.extend([
        "",
        "提示：上面是给人看的结论；下面的 JSON 是完整原始数据，方便你之后追溯每个指标。",
        "==============================",
        "",
    ])
    return "\n".join(lines)


def build_html_report(
    result: Dict[str, Any],
    report_version: Optional[Dict[str, str]] = None,
    generated_at: Optional[str] = None,
) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_version = report_version or {}
    source = result.get("position_source", {})
    recorded = result.get("recorded_positions", {})
    stage_1_by_symbol = build_stage_1_lookup(result.get("stage_1_position_analysis", []))
    recommendations = result.get("stage_2_operation_recommendations", [])

    position_sections = html_recorded_positions_section(recorded)

    recommendation_rows = build_html_recommendation_rows(stage_1_by_symbol, recommendations)
    overview_rows = recommendation_rows["overview_rows"]
    overview_classes = recommendation_rows["overview_classes"]
    priority_rows = recommendation_rows["priority_rows"]
    priority_classes = recommendation_rows["priority_classes"]
    sell_rows = recommendation_rows["sell_rows"]
    roll_rows = recommendation_rows["roll_rows"]
    put_payoff_rows = recommendation_rows["put_payoff_rows"]
    put_payoff_summaries = recommendation_rows["put_payoff_summaries"]
    put_advice_rows = recommendation_rows["put_advice_rows"]

    detail_cards: List[str] = []
    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        position = stage_1_by_symbol.get(analysis_lookup_key(recommendation), {})
        detail_cards.append(html_symbol_detail_card(recommendation, position))

    raw_json = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    css = html_report_css()

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PMCC Report</title>
  <style>{css}</style>
</head>
<body>
  <main class="wrap">
    {html_report_header(generated_at, report_version, source, len(recommendations))}

    {position_sections}

    {html_platform_ledger_sections(recommendation_rows)}

    <section class="grid">
      {"".join(detail_cards)}
    </section>

    {html_raw_json_section(raw_json)}
  </main>
</body>
</html>
"""


def render_html_report(
    result: Dict[str, Any],
    report_version: Optional[Dict[str, str]] = None,
    generated_at: Optional[str] = None,
) -> str:
    return build_html_report(result, report_version=report_version, generated_at=generated_at)


def write_html_report(
    path: Path,
    result: Dict[str, Any],
    report_version: Optional[Dict[str, str]] = None,
    generated_at: Optional[str] = None,
) -> Path:
    write_report_text(path, render_html_report(result, report_version=report_version, generated_at=generated_at))
    return path


def html_symbol_detail_card(recommendation: Dict[str, Any], position: Dict[str, Any]) -> str:
    symbol = recommendation.get("symbol") or position.get("symbol") or "-"
    portfolio = portfolio_display_name(recommendation)
    candidate = recommendation.get("candidate_short_call") or {}
    summary = recommendation.get("summary_cn") or ""
    long_leg_analysis = position.get("long_leg_analysis") or {}
    warnings = long_leg_analysis.get("warnings") or []
    warning_items = "".join(f"<li>{html_text(item)}</li>" for item in warnings[:6]) or "<li>无明显底仓警告。</li>"
    long_leg_expiry_summary = format_long_leg_expiry_summary(long_leg_analysis) if long_leg_analysis else "Long-leg expiry risk: unavailable."
    long_leg_expiry_table = html_long_leg_expiry_table(long_leg_analysis)
    slot_analysis = candidate.get("leaps_coverage_slot_analysis") or position.get("leaps_coverage_slots") or {}
    slot_summary = format_leaps_slot_summary(slot_analysis)
    slot_table = html_leaps_slot_table(slot_analysis)
    roll_pnl_detail = html_roll_pnl_detail_section(recommendation)
    validation = position.get("data_validation") or {}
    event_block = position.get("event_block") or recommendation.get("event_block") or candidate.get("event_block") or {}
    event_summary = summarize_event_block(event_block) if event_block else "event block unavailable"
    event_class = event_risk_class(event_block)
    validation_class = validation_risk_class(validation)
    validation_items = "".join(
        f"<li>{html_text((item.get('name') or item.get('source')) + ': ' + str(item.get('status')) + (' - ' + str(item.get('error')) if item.get('error') else ''))}</li>"
        for item in validation.get("sources", [])
    ) or "<li>无备用数据源记录。</li>"
    return f"""
            <section class="card">
              <h2>{html_text(portfolio)} · {html_text(symbol)}</h2>
              <p class="summary">{html_text(summary)}</p>
              <div class="metrics">
                <span>现价 <strong>{html_value(position.get("price"), 2)}</strong></span>
                <span>趋势 <strong>{html_text(explain_trend(position.get("trend")))}</strong></span>
                <span>IV Rank <strong>{html_value(position.get("iv_rank_est"), 1)}</strong></span>
                <span>IV Rank 来源 <strong>{html_text(summarize_iv_rank_analysis(position.get("iv_rank_analysis") or {}))}</strong></span>
                <span>IV 环境 <strong>{html_text(summarize_iv_environment(position.get("iv_environment") or {}))}</strong></span>
                <span>候选 <strong>{html_text(candidate.get("code") or "-")}</strong></span>
              </div>
              <h3>底仓提醒</h3>
              <ul>{warning_items}</ul>
              <h3>Long-leg expiry Greeks</h3>
              <p class="muted">{html_text(long_leg_expiry_summary)}</p>
              {long_leg_expiry_table}
              <h3>逐张 LEAPS 覆盖槽位</h3>
              <p class="muted">{html_text(slot_summary)}</p>
              {slot_table}
              {roll_pnl_detail}
              <h3>备用数据源校验</h3>
              <p class="validation-banner {validation_class}">{html_text(summarize_data_validation(validation))}</p>
              <ul>{validation_items}</ul>
              <h3>{html_text(symbol)} 事件窗口检查</h3>
              <p class="event-banner {event_class}">{html_text(event_summary)}</p>
            </section>
            """


def html_report_header(
    generated_at: str,
    report_version: Dict[str, str],
    source: Dict[str, Any],
    recommendation_count: int,
) -> str:
    return f"""
    <header>
      <div>
        <h1>PMCC 交易报告</h1>
        <div class="subtitle">生成时间：{html_text(generated_at)}</div>
        <div class="subtitle">分支：{html_text(report_version.get("branch"))}；版本：{html_text(report_version.get("version"))}</div>
      </div>
      <div class="kpis">
        <div class="kpi"><span>底仓合约</span><strong>{html_value(source.get("total_base_contracts"), 0)}</strong></div>
        <div class="kpi"><span>已卖 Call</span><strong>{html_value(source.get("total_short_call_contracts"), 0)}</strong></div>
        <div class="kpi"><span>已卖 Put</span><strong>{html_value(source.get("total_short_put_contracts"), 0)}</strong></div>
        <div class="kpi"><span>分析标的</span><strong>{recommendation_count}</strong></div>
      </div>
    </header>
"""


def html_report_css() -> str:
    return """
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#ffffff; --text:#172033; --muted:#667085; --line:#d9dee8; --blue:#1f5eff; --red:#b42318; --orange:#b54708; --yellow:#8a6100; --green:#067647; --gray:#475467; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: "Segoe UI", Arial, sans-serif; background:var(--bg); color:var(--text); }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 28px; }
    header { display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:22px; }
    h1 { margin:0 0 8px; font-size:30px; letter-spacing:0; }
    h2 { margin:0 0 14px; font-size:20px; }
    h3 { margin:18px 0 8px; font-size:15px; }
    h4 { margin:14px 0 6px; font-size:13px; color:var(--muted); }
    .subtitle, .muted { color:var(--muted); }
    .kpis { display:grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap:12px; min-width:560px; }
    .kpi, .card, .platform-ledger { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; box-shadow:0 1px 2px rgba(16,24,40,.04); }
    .kpi span { display:block; color:var(--muted); font-size:13px; }
    .kpi strong { display:block; font-size:26px; margin-top:6px; }
    .grid { display:grid; grid-template-columns: 1fr; gap:18px; }
    table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    th, td { padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:14px; }
    th { background:#eef2f8; color:#344054; font-weight:650; }
    tr.group-row.group-0 td { background:#ffffff; }
    tr.group-row.group-1 td { background:#f8fafc; }
    tr.group-row + tr.group-row.group-0 td, tr.group-row + tr.group-row.group-1 td { border-top:1px solid #d5dce8; }
    tr.group-row.group-0 + tr.group-row.group-0 td, tr.group-row.group-1 + tr.group-row.group-1 td { border-top:0; }
    tr.group-row:hover td { background:#eef6ff; }
    tr:last-child td { border-bottom:0; }
    tr.risk-red td:first-child { color:var(--red); font-weight:700; }
    tr.risk-orange td:first-child { color:var(--orange); font-weight:700; }
    tr.risk-yellow td:first-child { color:var(--yellow); font-weight:700; }
    tr.risk-green td:first-child { color:var(--green); font-weight:700; }
    tr.risk-gray td:first-child { color:var(--gray); font-weight:700; }
    .section { margin:18px 0; }
    .section-title { display:flex; align-items:center; justify-content:space-between; margin:0 0 10px; }
    .platform-ledger { border-top:3px solid var(--blue); }
    .platform-ledger + .platform-ledger { margin-top:24px; }
    .metrics { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
    .metrics span { background:#f2f4f7; border:1px solid var(--line); border-radius:6px; padding:8px 10px; }
    .event-banner, .validation-banner { border:1px solid var(--line); border-left-width:6px; border-radius:8px; padding:12px 14px; font-weight:700; }
    .event-blocked, .validation-warn { background:#fff1f0; border-color:#fecdca; border-left-color:var(--red); color:var(--red); }
    .event-attention, .validation-attention { background:#fff7ed; border-color:#fed7aa; border-left-color:var(--orange); color:var(--orange); }
    .event-normal, .validation-ok { background:#f2f4f7; border-color:var(--line); border-left-color:var(--gray); color:var(--gray); font-weight:600; }
    .validation-unavailable { background:#fffbeb; border-color:#fde68a; border-left-color:var(--yellow); color:var(--yellow); }
    .summary { line-height:1.6; }
    details { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }
    summary { cursor:pointer; font-weight:650; }
    pre { white-space:pre-wrap; word-break:break-word; font-size:12px; color:#101828; }
    .empty { color:var(--muted); text-align:center; }
    @media (max-width: 760px) { .wrap { padding:16px; } header { display:block; } .kpis { grid-template-columns:1fr; min-width:0; margin-top:16px; } table { display:block; overflow-x:auto; } }
    """


def analysis_lookup_key(item: Dict[str, Any]) -> str:
    symbol = item.get("symbol") or "-"
    portfolio_id = item.get("portfolio_id")
    if portfolio_id:
        return f"{portfolio_id}|{symbol}"
    return str(symbol)


def build_stage_1_lookup(stage_1_items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for item in stage_1_items:
        if item.get("symbol"):
            lookup[analysis_lookup_key(item)] = item
    return lookup


def portfolio_display_name(item: Dict[str, Any]) -> str:
    return str(item.get("portfolio_label") or item.get("portfolio_id") or "-")


def html_recorded_positions_section(recorded: Dict[str, Any]) -> str:
    portfolios = recorded.get("portfolios") or []
    if not portfolios:
        position_rows = build_html_position_rows(recorded.get("base_positions", []), recorded.get("short_calls", []))
        return html_positions_section(
            position_rows["base_rows"],
            position_rows["short_call_rows"],
            position_rows["short_put_rows"],
            position_rows["other_short_rows"],
        )

    sections: List[str] = ['<section class="section"><h2>本次持仓确认</h2><p class="muted">按平台/账户分别确认；不同平台之间不互相覆盖。</p>']
    for portfolio in portfolios:
        position_rows = build_html_position_rows(portfolio.get("base_positions", []), portfolio.get("short_calls", []))
        label = portfolio_display_name(portfolio)
        sections.append(f"<h3>{html_text(label)}</h3>")
        sections.append("<h4>LEAPS / 底仓</h4>")
        sections.append(html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], position_rows["base_rows"]))
        sections.append("<h4>已卖 Short Call</h4>")
        sections.append(html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], position_rows["short_call_rows"]))
        sections.append("<h4>已卖 Short Put / CSP</h4>")
        sections.append(html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], position_rows["short_put_rows"]))
        sections.append("<h4>其他 Short Option</h4>")
        sections.append(html_table(["标的", "合约代码", "数量", "行权价", "到期日", "类型", "成本"], position_rows["other_short_rows"]))
    sections.append("</section>")
    return "\n".join(sections)


def split_rows_by_portfolio(rows: List[List[Any]], classes: Optional[List[str]] = None, portfolio_index: int = 0) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    index_by_name: Dict[str, int] = {}
    for index, row in enumerate(rows):
        portfolio = str(row[portfolio_index] if len(row) > portfolio_index else "-")
        if portfolio not in index_by_name:
            index_by_name[portfolio] = len(grouped)
            grouped.append({"portfolio": portfolio, "rows": [], "classes": []})
        group = grouped[index_by_name[portfolio]]
        group["rows"].append(row[:portfolio_index] + row[portfolio_index + 1 :])
        if classes is not None:
            group["classes"].append(classes[index])
    return grouped


def html_platform_ledger_sections(recommendation_rows: Dict[str, Any]) -> str:
    overview_groups = split_rows_by_portfolio(recommendation_rows["overview_rows"], recommendation_rows["overview_classes"])
    priority_groups = split_rows_by_portfolio(recommendation_rows["priority_rows"], recommendation_rows["priority_classes"], portfolio_index=2)
    sell_groups = split_rows_by_portfolio(recommendation_rows["sell_rows"])
    roll_groups = split_rows_by_portfolio(recommendation_rows["roll_rows"])
    put_advice_groups = split_rows_by_portfolio(recommendation_rows["put_advice_rows"])
    put_payoff_groups = split_rows_by_portfolio(recommendation_rows["put_payoff_rows"])
    portfolios = [group["portfolio"] for group in overview_groups]
    for groups in [priority_groups, sell_groups, roll_groups, put_advice_groups, put_payoff_groups]:
        for group in groups:
            if group["portfolio"] not in portfolios:
                portfolios.append(group["portfolio"])

    sections: List[str] = []
    for portfolio in portfolios:
        overview = next((group for group in overview_groups if group["portfolio"] == portfolio), {"rows": [], "classes": []})
        priority = next((group for group in priority_groups if group["portfolio"] == portfolio), {"rows": [], "classes": []})
        sell = next((group for group in sell_groups if group["portfolio"] == portfolio), {"rows": []})
        roll = next((group for group in roll_groups if group["portfolio"] == portfolio), {"rows": []})
        put_advice = next((group for group in put_advice_groups if group["portfolio"] == portfolio), {"rows": []})
        put_payoff = next((group for group in put_payoff_groups if group["portfolio"] == portfolio), {"rows": []})
        sections.append(
            f"""
    <section class="section platform-ledger">
      <div class="section-title"><h2>{html_text(portfolio)}</h2><span class="muted">同平台内独立判断覆盖、ROLL 和新增卖出</span></div>
      {html_table(["标的", "现价", "趋势", "IVR", "IV环境", "底仓", "已卖Call", "可卖Call", "风险灯", "数据校验", "新增卖CALL"], overview["rows"], overview["classes"])}
      <h3>今日优先事项</h3>
      {html_table(["Light", "State", "Symbol", "Type", "Short Leg", "Strike", "DTE", "Delta", "Distance%", "Profit%", "Action"], priority["rows"], priority["classes"])}
      <h3>新增卖 CALL 判断</h3>
      {html_table(["标的", "结论", "候选合约", "行权价", "DTE", "Delta", "可卖", "保护线", "Liquidity"], sell["rows"])}
      <h3>Short Put Expiration P/L</h3>
      {html_table(["Symbol", "Put", "Action Advice", "Next Trigger"], put_advice["rows"])}
      {html_table(["Symbol", "Put", "Spot@Exp", "Spot Chg%", "Intrinsic", "P/L per sh", "P/L total", "Assigned", "Eff. Cost"], put_payoff["rows"])}
      <h3>ROLL 候选表</h3>
      {html_table(["标的", "原合约", "序", "Roll到", "行权价", "DTE", "Delta", "上移", "估算净额", "Roll现金流", "整体P/L"], roll["rows"])}
    </section>
"""
        )

    if not sections:
        return html_overview_section([], [])
    return "\n".join(sections)


def html_positions_section(
    base_rows: List[List[Any]],
    short_call_rows: List[List[Any]],
    short_put_rows: List[List[Any]],
    other_short_rows: List[List[Any]],
) -> str:
    return f"""
    <section class="section">
      <h2>本次持仓确认</h2>
      <h3>LEAPS / 底仓</h3>
      {html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], base_rows)}
      <h3>已卖 Short Call</h3>
      {html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], short_call_rows)}
      <h3>已卖 Short Put / CSP</h3>
      {html_table(["标的", "合约代码", "数量", "行权价", "到期日", "成本"], short_put_rows)}
      <h3>其他 Short Option</h3>
      {html_table(["标的", "合约代码", "数量", "行权价", "到期日", "类型", "成本"], other_short_rows)}
    </section>
"""


def html_overview_section(overview_rows: List[List[Any]], overview_classes: List[str]) -> str:
    return f"""
    <section class="section">
      <div class="section-title"><h2>平台分组总览</h2><span class="muted">先看风险灯，再看动作；覆盖能力只在同平台内计算</span></div>
      {html_table(["平台", "标的", "现价", "趋势", "IVR", "IV环境", "底仓", "已卖Call", "可卖Call", "风险灯", "数据校验", "新增卖CALL"], overview_rows, overview_classes)}
    </section>
"""


def html_priority_section(priority_rows: List[List[Any]], priority_classes: List[str]) -> str:
    return f"""
    <section class="section">
      <h2>今日优先事项</h2>
      {html_table(["Light", "State", "Portfolio", "Symbol", "Type", "Short Leg", "Strike", "DTE", "Delta", "Distance%", "Profit%", "Action"], priority_rows, priority_classes)}
    </section>
"""


def html_short_put_payoff_section(
    put_advice_rows: List[List[Any]],
    put_payoff_rows: List[List[Any]],
    put_payoff_summaries: List[str],
) -> str:
    summary = " | ".join(put_payoff_summaries) if put_payoff_summaries else "No short put payoff scenarios available."
    return f"""
    <section class="section">
      <div class="section-title"><h2>Short Put Expiration P/L</h2><span class="muted">OptionLab-style static payoff scenarios</span></div>
      {html_table(["Portfolio", "Symbol", "Put", "Action Advice", "Next Trigger"], put_advice_rows)}
      {html_table(["Portfolio", "Symbol", "Put", "Spot@Exp", "Spot Chg%", "Intrinsic", "P/L per sh", "P/L total", "Assigned", "Eff. Cost"], put_payoff_rows)}
      <p class="muted">{html_text(summary)}</p>
    </section>
"""


def html_sell_section(sell_rows: List[List[Any]]) -> str:
    return f"""
    <section class="section">
      <h2>新增卖CALL判断</h2>
      {html_table(["平台", "标的", "结论", "候选合约", "行权价", "DTE", "Delta", "可卖", "保护线", "Liquidity"], sell_rows)}
    </section>
"""


def html_roll_section(roll_rows: List[List[Any]]) -> str:
    return f"""
    <section class="section">
      <h2>ROLL候选表</h2>
      {html_table(["平台", "标的", "原合约", "序", "Roll到", "行权价", "DTE", "Delta", "上移", "估算净额", "Roll现金流", "整体P/L"], roll_rows)}
      <p class="muted">估算净额中负数表示净支出，正数表示净收入。</p>
    </section>
"""


def html_raw_json_section(raw_json: str) -> str:
    return f"""
    <section class="section">
      <details>
        <summary>完整 JSON 原始数据</summary>
        <pre>{html_text(raw_json)}</pre>
      </details>
    </section>
"""


def build_html_position_rows(base_positions: List[Dict[str, Any]], short_calls: List[Dict[str, Any]]) -> Dict[str, List[List[Any]]]:
    return {
        "base_rows": [
            [
                item.get("underlying") or "-",
                item.get("code") or "-",
                item.get("quantity") or "-",
                format_value(item.get("strike"), 2),
                item.get("expiry") or "-",
                format_value(item.get("cost_price"), 2),
            ]
            for item in base_positions
        ],
        "short_call_rows": [
            [
                item.get("underlying") or "-",
                item.get("code") or "-",
                item.get("quantity") or "-",
                format_value(item.get("strike"), 2),
                item.get("expiry") or "-",
                format_value(item.get("cost_price"), 2),
            ]
            for item in short_calls
            if item.get("option_type") == "CALL"
        ],
        "short_put_rows": [
            [
                item.get("underlying") or "-",
                item.get("code") or "-",
                item.get("quantity") or "-",
                format_value(item.get("strike"), 2),
                item.get("expiry") or "-",
                format_value(item.get("cost_price"), 2),
            ]
            for item in short_calls
            if item.get("option_type") == "PUT"
        ],
        "other_short_rows": [
            [
                item.get("underlying") or "-",
                item.get("code") or "-",
                item.get("quantity") or "-",
                format_value(item.get("strike"), 2),
                item.get("expiry") or "-",
                item.get("option_type") or "-",
                format_value(item.get("cost_price"), 2),
            ]
            for item in short_calls
            if item.get("option_type") not in {"CALL", "PUT"}
        ],
    }


def build_html_recommendation_rows(
    stage_1_by_symbol: Dict[str, Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    overview_rows: List[List[Any]] = []
    overview_classes: List[str] = []
    priority_rows: List[List[Any]] = []
    priority_classes: List[str] = []
    sell_rows: List[List[Any]] = []
    roll_rows: List[List[Any]] = []
    put_payoff_rows: List[List[Any]] = []
    put_payoff_summaries: List[str] = []
    put_advice_rows: List[List[Any]] = []

    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        position = stage_1_by_symbol.get(analysis_lookup_key(recommendation), {})
        portfolio = portfolio_display_name(recommendation)
        coverage = position.get("coverage") or {}
        light, state = symbol_risk_light(recommendation)
        overview_rows.append(
            [
                portfolio,
                symbol,
                format_value(position.get("price"), 2),
                explain_trend(position.get("trend")),
                format_value(position.get("iv_rank_est"), 1),
                summarize_iv_environment(position.get("iv_environment") or {}),
                format_value(coverage.get("base_contracts"), 0),
                format_value(coverage.get("short_call_contracts"), 0),
                format_value(coverage.get("available_to_sell"), 0),
                f"{light} {state}",
                summarize_data_validation(position.get("data_validation") or {}),
                explain_action(recommendation.get("action")),
            ]
        )
        overview_classes.append(risk_class(light))

        candidate = recommendation.get("candidate_short_call") or {}
        candidate_capacity = effective_candidate_capacity(candidate, coverage)
        safety = candidate.get("mcmillan_safety") or "-"
        if safety in {"BLOCKED_BELOW_COST_LINE", "BLOCKED_NO_ELIGIBLE_LEAPS_SLOT"}:
            safety = "低于保护线"
        elif safety in {"OK_ABOVE_COST_LINE", "OK_HAS_ELIGIBLE_LEAPS_SLOTS"}:
            safety = "保护线以上"
        sell_rows.append(
            [
                portfolio,
                symbol,
                explain_action(recommendation.get("action")),
                candidate.get("code") or "-",
                format_value(candidate.get("strike"), 2),
                format_value(candidate.get("days_to_expiry"), 0),
                format_value(candidate.get("delta"), 3),
                format_value(candidate_capacity, 0),
                safety,
                format_liquidity_summary(candidate),
            ]
        )

        for review in recommendation.get("short_call_reviews") or []:
            review_light, review_state = short_call_risk_light(review)
            priority_rows.append(
                [
                    review_light,
                    review_state,
                    portfolio,
                    symbol,
                    "short_call",
                    review.get("code") or "-",
                    format_value(review.get("strike"), 2),
                    format_value(review.get("days_to_expiry"), 0),
                    format_value(review.get("delta"), 3),
                    format_value(review.get("price_to_strike_pct"), 2),
                    format_value(review.get("profit_capture_pct"), 1),
                    explain_action(review.get("roll_action")),
                ]
            )
            priority_classes.append(risk_class(review_light))
            for index, roll_candidate in enumerate((review.get("roll_candidates") or [])[:5], start=1):
                roll_rows.append(
                    [
                        portfolio,
                        symbol,
                        review.get("code") or "-",
                        index,
                        roll_candidate.get("code") or "-",
                        format_value(roll_candidate.get("strike"), 2),
                        format_value(roll_candidate.get("days_to_expiry"), 0),
                        format_value(roll_candidate.get("delta"), 3),
                        format_value(roll_candidate.get("strike_lift"), 2),
                        format_net_roll_price(roll_candidate),
                        format_roll_pnl_value(roll_candidate, "roll_net_cashflow_est"),
                        format_roll_pnl_value(roll_candidate, "symbol_total_pnl_after_roll_est"),
                    ]
                )
        for review in recommendation.get("short_put_reviews") or []:
            review_light, review_state = short_put_risk_light(review)
            priority_rows.append(
                [
                    review_light,
                    review_state,
                    portfolio,
                    symbol,
                    "short_put",
                    review.get("code") or "-",
                    format_value(review.get("strike"), 2),
                    format_value(review.get("days_to_expiry"), 0),
                    format_value(review.get("delta"), 3),
                    format_value(review.get("underlying_to_strike_pct"), 2),
                    format_value(review.get("profit_capture_pct"), 1),
                    explain_action(review.get("roll_action")),
                ]
            )
            priority_classes.append(risk_class(review_light))
            payoff = review.get("payoff_scenarios") or {}
            put_advice_rows.append(
                [
                    portfolio,
                    symbol,
                    review.get("code") or "-",
                    review.get("operation_advice_text") or build_short_put_operation_advice(review),
                    (review.get("wheel_state") or {}).get("next_check_trigger") or "-",
                ]
            )
            if payoff.get("summary"):
                put_payoff_summaries.append(f"{symbol} {review.get('code')}: {payoff.get('summary')}")
            for scenario in (payoff.get("rows") or []):
                put_payoff_rows.append(
                    [
                        portfolio,
                        symbol,
                        review.get("code") or "-",
                        format_value(scenario.get("spot_at_expiry"), 2),
                        format_value(scenario.get("spot_change_pct"), 2),
                        format_value(scenario.get("put_intrinsic_at_expiry"), 2),
                        format_value(scenario.get("pnl_per_share"), 2),
                        format_value(scenario.get("pnl_total"), 2),
                        "YES" if scenario.get("assigned") else "NO",
                        format_value(scenario.get("effective_share_cost"), 2, empty="-"),
                    ]
                )
            for index, roll_candidate in enumerate((review.get("roll_candidates") or [])[:5], start=1):
                roll_rows.append(
                    [
                        portfolio,
                        symbol,
                        review.get("code") or "-",
                        index,
                        roll_candidate.get("code") or "-",
                        format_value(roll_candidate.get("strike"), 2),
                        format_value(roll_candidate.get("days_to_expiry"), 0),
                        format_value(roll_candidate.get("delta"), 3),
                        format_value(roll_candidate.get("strike_change"), 2),
                        format_net_roll_price(roll_candidate),
                        format_roll_pnl_value(roll_candidate, "roll_net_cashflow_est"),
                        format_roll_pnl_value(roll_candidate, "symbol_total_pnl_after_roll_est"),
                    ]
                )

    return {
        "overview_rows": overview_rows,
        "overview_classes": overview_classes,
        "priority_rows": priority_rows,
        "priority_classes": priority_classes,
        "sell_rows": sell_rows,
        "roll_rows": roll_rows,
        "put_payoff_rows": put_payoff_rows,
        "put_payoff_summaries": put_payoff_summaries,
        "put_advice_rows": put_advice_rows,
    }


def build_trader_summary_report(result: Dict[str, Any]) -> List[str]:
    if result.get("mode") not in {"pmcc_two_stage", "pmcc_two_stage_multi_portfolio"}:
        return []

    stage_1_by_symbol = build_stage_1_lookup(result.get("stage_1_position_analysis", []))
    recommendations = result.get("stage_2_operation_recommendations", [])

    lines = ["", "========== 交易员版摘要 =========="]
    overview_rows: List[List[Any]] = []
    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        position = stage_1_by_symbol.get(analysis_lookup_key(recommendation), {})
        portfolio = portfolio_display_name(recommendation)
        coverage = position.get("coverage") or {}
        light, state = symbol_risk_light(recommendation)
        overview_rows.append(
            [
                portfolio,
                symbol,
                format_value(position.get("price"), 2),
                explain_trend(position.get("trend")),
                format_value(position.get("iv_rank_est"), 1),
                summarize_iv_environment(position.get("iv_environment") or {}),
                format_value(coverage.get("base_contracts"), 0),
                format_value(coverage.get("short_call_contracts"), 0),
                format_value(coverage.get("available_to_sell"), 0),
                f"{light} {state}",
                summarize_data_validation(position.get("data_validation") or {}),
                explain_action(recommendation.get("action")),
            ]
        )

    lines.append("")
    lines.append("【账户总览】")
    lines.extend(
        format_terminal_table(
            ["平台", "标的", "现价", "趋势", "IVR", "IV环境", "底仓", "已卖", "可卖", "风险灯", "数据校验", "新增卖CALL"],
            overview_rows,
        )
    )

    priority_rows: List[List[Any]] = []
    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        portfolio = portfolio_display_name(recommendation)
        for review in recommendation.get("short_call_reviews") or []:
            light, state = short_call_risk_light(review)
            priority_rows.append(
                [
                    light,
                    state,
                    portfolio,
                    symbol,
                    "short_call",
                    review.get("code") or "-",
                    format_value(review.get("strike"), 2),
                    format_value(review.get("days_to_expiry"), 0),
                    format_value(review.get("delta"), 3),
                    format_value(review.get("price_to_strike_pct"), 2),
                    format_value(review.get("profit_capture_pct"), 1),
                    explain_action(review.get("roll_action")),
                ]
            )
        for review in recommendation.get("short_put_reviews") or []:
            light, state = short_put_risk_light(review)
            priority_rows.append(
                [
                    light,
                    state,
                    portfolio,
                    symbol,
                    "short_put",
                    review.get("code") or "-",
                    format_value(review.get("strike"), 2),
                    format_value(review.get("days_to_expiry"), 0),
                    format_value(review.get("delta"), 3),
                    format_value(review.get("underlying_to_strike_pct"), 2),
                    format_value(review.get("profit_capture_pct"), 1),
                    explain_action(review.get("roll_action")),
                ]
            )
    priority_rows.sort(key=lambda row: risk_rank(row[0]), reverse=True)

    lines.append("")
    lines.append("【今日优先事项】")
    if priority_rows:
        lines.extend(
            format_terminal_table(
                ["Light", "State", "Portfolio", "Symbol", "Type", "Short Leg", "Strike", "DTE", "Delta", "Distance%", "Profit%", "Action"],
                priority_rows,
            )
        )
    else:
        lines.append("没有需要处理的现有 short call。")

    sell_rows: List[List[Any]] = []
    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        position = stage_1_by_symbol.get(analysis_lookup_key(recommendation), {})
        portfolio = portfolio_display_name(recommendation)
        coverage = position.get("coverage") or {}
        candidate = recommendation.get("candidate_short_call") or {}
        candidate_capacity = effective_candidate_capacity(candidate, coverage)
        safety = candidate.get("mcmillan_safety") or "-"
        if safety in {"BLOCKED_BELOW_COST_LINE", "BLOCKED_NO_ELIGIBLE_LEAPS_SLOT"}:
            safety = "低于保护线"
        elif safety in {"OK_ABOVE_COST_LINE", "OK_HAS_ELIGIBLE_LEAPS_SLOTS"}:
            safety = "保护线以上"
        sell_rows.append(
            [
                portfolio,
                symbol,
                explain_action(recommendation.get("action")),
                candidate.get("code") or "-",
                format_value(candidate.get("strike"), 2),
                format_value(candidate.get("days_to_expiry"), 0),
                format_value(candidate.get("delta"), 3),
                format_value(candidate_capacity, 0),
                safety,
                format_liquidity_summary(candidate),
            ]
        )

    lines.append("")
    lines.append("【新增卖CALL判断】")
    lines.extend(
        format_terminal_table(
            ["平台", "标的", "结论", "候选合约", "行权价", "DTE", "Delta", "可卖", "保护线", "Liquidity"],
            sell_rows,
        )
    )

    roll_rows: List[List[Any]] = []
    for recommendation in recommendations:
        symbol = recommendation.get("symbol") or "-"
        portfolio = portfolio_display_name(recommendation)
        for review in recommendation.get("short_call_reviews") or []:
            for index, candidate in enumerate((review.get("roll_candidates") or [])[:3], start=1):
                roll_rows.append(
                    [
                        portfolio,
                        symbol,
                        review.get("code") or "-",
                        index,
                        candidate.get("code") or "-",
                        format_value(candidate.get("strike"), 2),
                        format_value(candidate.get("days_to_expiry"), 0),
                        format_value(candidate.get("delta"), 3),
                        format_value(candidate.get("strike_lift"), 2),
                        format_net_roll_price(candidate),
                        format_roll_pnl_value(candidate, "roll_net_cashflow_est"),
                        format_roll_pnl_value(candidate, "symbol_total_pnl_after_roll_est"),
                    ]
                )
        for review in recommendation.get("short_put_reviews") or []:
            for index, candidate in enumerate((review.get("roll_candidates") or [])[:3], start=1):
                roll_rows.append(
                    [
                        portfolio,
                        symbol,
                        review.get("code") or "-",
                        index,
                        candidate.get("code") or "-",
                        format_value(candidate.get("strike"), 2),
                        format_value(candidate.get("days_to_expiry"), 0),
                        format_value(candidate.get("delta"), 3),
                        format_value(candidate.get("strike_change"), 2),
                        format_net_roll_price(candidate),
                        format_roll_pnl_value(candidate, "roll_net_cashflow_est"),
                        format_roll_pnl_value(candidate, "symbol_total_pnl_after_roll_est"),
                    ]
                )

    lines.append("")
    lines.append("【ROLL候选表】")
    if roll_rows:
        lines.extend(
            format_terminal_table(
                ["平台", "标的", "原合约", "序", "Roll到", "行权价", "DTE", "Delta", "上移", "估算净额", "Roll现金流", "整体P/L"],
                roll_rows,
            )
        )
        lines.append("注：估算净额中负数表示净支出，正数表示净收入。")
    else:
        lines.append("当前没有 roll 候选。")

    lines.append("==================================")
    return lines
