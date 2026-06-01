from typing import Any, Dict, List, Optional

from pmcc.utils import safe_float, safe_int, safe_text


CONTRACT_MULTIPLIER = 100


def estimate_whole_symbol_roll_pnl(
    existing_short_call: Dict[str, Any],
    roll_candidate: Dict[str, Any],
    long_leg_analysis: Dict[str, Any],
    stock_price: Optional[float] = None,
    portfolio_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    identity = _normalize_identity(portfolio_identity or {})
    quantity = safe_int(existing_short_call.get("quantity")) or 0
    old_credit = safe_float(existing_short_call.get("cost_price") or existing_short_call.get("open_credit"))
    old_buyback = safe_float(
        roll_candidate.get("estimated_buyback_price")
        or existing_short_call.get("current_buyback_price")
        or existing_short_call.get("mark_price")
    )
    candidate_credit = safe_float(roll_candidate.get("bid_price") or roll_candidate.get("open_credit") or roll_candidate.get("mark_price"))
    costs = _total_costs(existing_short_call, roll_candidate)
    missing: List[str] = _identity_mismatches(identity, existing_short_call, roll_candidate, long_leg_analysis)

    old_short_realized = None
    if quantity <= 0:
        missing.append("short_call_quantity")
    if old_credit is None:
        missing.append("old_short_open_credit")
    if old_buyback is None:
        missing.append("old_short_buyback_price")
    if candidate_credit is None:
        missing.append("candidate_open_credit")

    if quantity > 0 and old_credit is not None and old_buyback is not None:
        old_short_realized = (old_credit - old_buyback) * quantity * CONTRACT_MULTIPLIER - costs["close_costs"]

    roll_net_cashflow = None
    if quantity > 0 and old_buyback is not None and candidate_credit is not None:
        roll_net_cashflow = (candidate_credit - old_buyback) * quantity * CONTRACT_MULTIPLIER - costs["total_costs"]

    long_pnl_result = estimate_long_leg_unrealized_pnl(long_leg_analysis)
    missing.extend(long_pnl_result["missing_pnl_inputs"])

    current_open_short_pnl = old_short_realized
    before_roll = None
    after_roll = None
    if not missing and long_pnl_result["long_leg_unrealized_pnl_est"] is not None and current_open_short_pnl is not None:
        before_roll = long_pnl_result["long_leg_unrealized_pnl_est"] + current_open_short_pnl
        after_roll = long_pnl_result["long_leg_unrealized_pnl_est"] + old_short_realized

    scenarios = []
    if not missing:
        scenarios = build_after_roll_scenarios(
            existing_short_call,
            roll_candidate,
            long_leg_analysis,
            old_short_realized,
            candidate_credit,
            stock_price,
        )

    return {
        "portfolio_identity": identity,
        "old_short_realized_pnl_est": _round_money(old_short_realized),
        "roll_net_cashflow_est": _round_money(roll_net_cashflow),
        "long_leg_unrealized_pnl_est": _round_money(long_pnl_result["long_leg_unrealized_pnl_est"]),
        "symbol_total_pnl_before_roll_est": _round_money(before_roll),
        "symbol_total_pnl_after_roll_est": _round_money(after_roll),
        "current_open_short_leg_pnl_est": _round_money(current_open_short_pnl),
        "pnl_components_available": not missing,
        "missing_pnl_inputs": sorted(set(missing)),
        "after_roll_scenarios": scenarios,
        "pnl_warning": _pnl_warning(missing, scenarios),
    }


def estimate_long_leg_unrealized_pnl(long_leg_analysis: Dict[str, Any]) -> Dict[str, Any]:
    total = 0.0
    has_value = False
    missing: List[str] = []
    for leg in long_leg_analysis.get("legs") or []:
        code = safe_text(leg.get("code")) or "long_leg"
        quantity = safe_int(leg.get("quantity"))
        mark = safe_float(leg.get("mark_price"))
        cost = safe_float(leg.get("cost_price"))
        if quantity is None or quantity <= 0:
            missing.append(f"{code}.quantity")
            continue
        if mark is None:
            missing.append(f"{code}.mark_price")
            continue
        if cost is None:
            missing.append(f"{code}.cost_price")
            continue
        total += (mark - cost) * quantity * CONTRACT_MULTIPLIER
        has_value = True
    return {
        "long_leg_unrealized_pnl_est": total if has_value and not missing else None,
        "missing_pnl_inputs": missing,
    }


def build_after_roll_scenarios(
    existing_short_call: Dict[str, Any],
    roll_candidate: Dict[str, Any],
    long_leg_analysis: Dict[str, Any],
    old_short_realized: Optional[float],
    candidate_credit: Optional[float],
    stock_price: Optional[float],
) -> List[Dict[str, Any]]:
    spot = safe_float(stock_price)
    new_strike = safe_float(roll_candidate.get("strike"))
    old_strike = safe_float(existing_short_call.get("strike"))
    quantity = safe_int(existing_short_call.get("quantity")) or 0
    if spot is None or new_strike is None or old_short_realized is None or candidate_credit is None or quantity <= 0:
        return []

    scenario_spots = [spot * 0.95, spot]
    if old_strike is not None:
        scenario_spots.append(old_strike)
    scenario_spots.extend([new_strike, spot * 1.05])
    unique_spots = sorted({round(value, 2) for value in scenario_spots if value > 0})

    rows: List[Dict[str, Any]] = []
    for scenario_spot in unique_spots:
        long_intrinsic_pnl = _long_intrinsic_pnl_at_spot(long_leg_analysis, scenario_spot)
        if long_intrinsic_pnl is None:
            continue
        new_short_pnl = (candidate_credit - max(scenario_spot - new_strike, 0)) * quantity * CONTRACT_MULTIPLIER
        total = old_short_realized + long_intrinsic_pnl + new_short_pnl
        rows.append(
            {
                "spot": scenario_spot,
                "old_short_realized_pnl_est": _round_money(old_short_realized),
                "long_intrinsic_pnl_est": _round_money(long_intrinsic_pnl),
                "new_short_pnl_at_expiry_est": _round_money(new_short_pnl),
                "symbol_total_pnl_est": _round_money(total),
                "estimate_basis": "static_intrinsic_payoff",
            }
        )
    return rows


def _long_intrinsic_pnl_at_spot(long_leg_analysis: Dict[str, Any], spot: float) -> Optional[float]:
    total = 0.0
    has_value = False
    for leg in long_leg_analysis.get("legs") or []:
        strike = safe_float(leg.get("strike"))
        cost = safe_float(leg.get("cost_price"))
        quantity = safe_int(leg.get("quantity"))
        if strike is None or cost is None or quantity is None or quantity <= 0:
            return None
        total += (max(spot - strike, 0) - cost) * quantity * CONTRACT_MULTIPLIER
        has_value = True
    return total if has_value else None


def _total_costs(existing_short_call: Dict[str, Any], roll_candidate: Dict[str, Any]) -> Dict[str, float]:
    close_costs = (safe_float(existing_short_call.get("commission")) or 0.0) + (safe_float(existing_short_call.get("fees")) or 0.0)
    open_costs = (safe_float(roll_candidate.get("commission")) or 0.0) + (safe_float(roll_candidate.get("fees")) or 0.0)
    return {
        "close_costs": close_costs,
        "open_costs": open_costs,
        "total_costs": close_costs + open_costs,
    }


def _normalize_identity(identity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "broker": safe_text(identity.get("broker") or identity.get("portfolio_id")),
        "account": safe_text(identity.get("account")),
        "portfolio_id": safe_text(identity.get("portfolio_id")),
        "portfolio_label": safe_text(identity.get("portfolio_label")),
        "symbol": safe_text(identity.get("symbol")),
    }


def _identity_mismatches(
    identity: Dict[str, Any],
    existing_short_call: Dict[str, Any],
    roll_candidate: Dict[str, Any],
    long_leg_analysis: Dict[str, Any],
) -> List[str]:
    issues: List[str] = []
    for label, item in [
        ("existing_short_call", existing_short_call),
        ("roll_candidate", roll_candidate),
        ("long_leg_analysis", long_leg_analysis),
    ]:
        for field in ["broker", "account", "portfolio_id", "symbol"]:
            expected = safe_text(identity.get(field))
            actual = safe_text(item.get(field))
            if expected is not None and actual is not None and expected != actual:
                issues.append(f"{label}.{field}_mismatch")
    return issues


def _round_money(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def _pnl_warning(missing: List[str], scenarios: List[Dict[str, Any]]) -> Optional[str]:
    if missing:
        return "needs price/cost inputs: " + ", ".join(sorted(set(missing)))
    if not scenarios:
        return "scenario estimates unavailable"
    return None
