from typing import Any, Dict, List, Optional

import pandas as pd

from pmcc.models import ActionDecision, CandidateStructureAssessment, LiquidityAssessment, StrategyConfig
from pmcc.utils import safe_float, safe_int


def score_dte(days_to_expiry: Optional[int], config: StrategyConfig) -> float:
    if days_to_expiry is None:
        return -2.0
    if config.preferred_dte_min <= days_to_expiry <= config.preferred_dte_max:
        midpoint = (config.preferred_dte_min + config.preferred_dte_max) / 2
        return max(0.0, 12.0 - abs(days_to_expiry - midpoint) * 0.35)
    if days_to_expiry < config.preferred_dte_min:
        return max(-4.0, 8.0 - (config.preferred_dte_min - days_to_expiry) * 1.5)
    return max(-3.0, 8.0 - (days_to_expiry - config.preferred_dte_max) * 0.2)


def score_moneyness(otm_pct: Optional[float]) -> float:
    if otm_pct is None:
        return -3.0
    if 0.005 <= otm_pct <= 0.08:
        return max(0.0, 14.0 - abs(otm_pct - 0.03) * 180)
    if otm_pct < 0:
        return -8.0 + max(otm_pct * 25, -6.0)
    return max(-2.0, 8.0 - abs(otm_pct - 0.08) * 40)


def assess_option_liquidity(option: Any, config: StrategyConfig) -> Dict[str, Any]:
    bid = safe_float(option.get("bid_price"))
    ask = safe_float(option.get("ask_price"))
    last = safe_float(option.get("last_price"))
    volume = safe_int(option.get("volume"))
    open_interest = safe_int(option.get("open_interest"))
    reasons: List[str] = []

    mid = None
    spread = None
    spread_pct = None
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid if mid else None
    else:
        reasons.append("bid/ask unavailable or crossed")

    if spread_pct is not None and spread_pct > config.max_candidate_bid_ask_spread_pct:
        reasons.append(f"bid/ask spread {spread_pct * 100:.1f}% exceeds {config.max_candidate_bid_ask_spread_pct * 100:.1f}%")
    if open_interest is None or open_interest < config.min_candidate_open_interest:
        reasons.append(f"open interest {open_interest if open_interest is not None else 'N/A'} below {config.min_candidate_open_interest}")
    if volume is None or volume < config.min_candidate_volume:
        reasons.append(f"volume {volume if volume is not None else 'N/A'} below {config.min_candidate_volume}")

    last_mid_deviation_pct = None
    if last is not None and mid is not None and mid > 0:
        last_mid_deviation_pct = abs(last - mid) / mid
        if last_mid_deviation_pct > config.max_candidate_last_mid_deviation_pct:
            reasons.append(
                f"last/mid deviation {last_mid_deviation_pct * 100:.1f}% exceeds {config.max_candidate_last_mid_deviation_pct * 100:.1f}%"
            )

    return LiquidityAssessment(
        ok=not reasons,
        reasons=reasons,
        bid=bid,
        ask=ask,
        mid=mid,
        spread=spread,
        spread_pct=spread_pct,
        volume=volume,
        open_interest=open_interest,
        last_mid_deviation_pct=last_mid_deviation_pct,
    ).to_dict()


def score_options(options: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    scored = options.copy()
    delta_series = pd.to_numeric(scored["delta"], errors="coerce")
    has_delta = delta_series.notna()
    scored["dte_score"] = scored["days_to_expiry"].apply(lambda v: score_dte(safe_int(v), config))
    scored["moneyness_score"] = scored["otm_pct"].apply(lambda v: score_moneyness(safe_float(v)))
    scored["delta_score"] = -2.0

    if has_delta.any():
        midpoint = (config.target_delta_low + config.target_delta_high) / 2
        scored.loc[has_delta, "delta_score"] = (16.0 - (delta_series[has_delta] - midpoint).abs() * 100).clip(lower=-3.0)

    scored["selection_score"] = scored["moneyness_score"] + scored["dte_score"]
    if has_delta.any():
        scored.loc[has_delta, "selection_score"] = (
            scored.loc[has_delta, "moneyness_score"]
            + scored.loc[has_delta, "dte_score"]
            + scored.loc[has_delta, "delta_score"]
        )
    liquidity = scored.apply(lambda row: assess_option_liquidity(row, config), axis=1)
    scored["liquidity_ok"] = liquidity.apply(lambda item: bool(item.get("ok")))
    scored["liquidity_reasons"] = liquidity.apply(lambda item: item.get("reasons") or [])
    scored["bid_ask_spread_pct"] = liquidity.apply(lambda item: item.get("spread_pct"))
    scored["last_mid_deviation_pct"] = liquidity.apply(lambda item: item.get("last_mid_deviation_pct"))
    return scored


def select_option(options: pd.DataFrame, config: StrategyConfig) -> pd.Series:
    if options.empty:
        raise RuntimeError("No option data available after merging chain and greeks.")
    scored = score_options(options, config)
    delta_series = pd.to_numeric(scored["delta"], errors="coerce")
    dte_series = pd.to_numeric(scored["days_to_expiry"], errors="coerce")
    otm_series = pd.to_numeric(scored["otm_pct"], errors="coerce")
    dte_ok = dte_series.between(config.preferred_dte_min, config.preferred_dte_max, inclusive="both")
    otm_ok = otm_series >= 0
    candidates = scored[
        dte_ok
        & otm_ok
        & delta_series.between(config.target_delta_low, config.target_delta_high, inclusive="both")
        & scored["liquidity_ok"]
    ].copy()

    if not candidates.empty:
        return candidates.sort_values(by=["selection_score", "distance_from_spot", "strike_price"], ascending=[False, True, True]).iloc[0]

    fallback = scored[dte_ok & otm_ok & scored["liquidity_ok"]].copy()
    if fallback.empty:
        fallback = scored[
            dte_ok
            & otm_ok
            & delta_series.between(config.target_delta_low, config.target_delta_high, inclusive="both")
        ].copy()
    if fallback.empty:
        fallback = scored[dte_ok & otm_ok].copy()
    if fallback.empty:
        fallback = scored[dte_ok].copy()
    if fallback.empty:
        fallback = scored.copy()
    return fallback.sort_values(by=["selection_score", "is_otm", "distance_from_spot", "strike_price"], ascending=[False, False, True, True]).iloc[0]


def evaluate_candidate_structure(selected_option: pd.Series, config: StrategyConfig) -> Dict[str, Any]:
    selected_delta = safe_float(selected_option.get("delta"))
    selected_dte = safe_int(selected_option.get("days_to_expiry"))
    selected_otm_pct = safe_float(selected_option.get("otm_pct"))
    liquidity_ok = bool(selected_option.get("liquidity_ok"))
    checks = {
        "dte_in_target_range": selected_dte is not None and config.preferred_dte_min <= selected_dte <= config.preferred_dte_max,
        "slightly_otm_or_better": selected_otm_pct is not None and selected_otm_pct >= 0,
        "delta_in_target_range": selected_delta is not None and config.target_delta_low <= selected_delta <= config.target_delta_high,
        "greeks_available": selected_delta is not None,
        "liquidity_ok": liquidity_ok,
    }
    if checks["greeks_available"]:
        structure_ok = checks["dte_in_target_range"] and checks["slightly_otm_or_better"] and checks["delta_in_target_range"] and checks["liquidity_ok"]
    else:
        structure_ok = checks["dte_in_target_range"] and checks["slightly_otm_or_better"] and checks["liquidity_ok"]
    return CandidateStructureAssessment(
        structure_ok=structure_ok,
        checks=checks,
        liquidity_ok=liquidity_ok,
        liquidity_reasons=selected_option.get("liquidity_reasons") or [],
        bid_ask_spread_pct=safe_float(selected_option.get("bid_ask_spread_pct")),
        last_mid_deviation_pct=safe_float(selected_option.get("last_mid_deviation_pct")),
        volume=safe_int(selected_option.get("volume")),
        open_interest=safe_int(selected_option.get("open_interest")),
    ).to_dict()


def build_action(
    iv_rank: Optional[float],
    trend: str,
    stock_price: float,
    selected_option: pd.Series,
    config: StrategyConfig,
    iv_environment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action = "WAIT"
    reasons: List[str] = []
    selected_delta = safe_float(selected_option.get("delta"))
    selected_strike = safe_float(selected_option.get("strike_price"))
    selected_dte = safe_int(selected_option.get("days_to_expiry"))
    selected_otm_pct = safe_float(selected_option.get("otm_pct"))
    candidate_structure = evaluate_candidate_structure(selected_option, config)
    structure_ok = bool(candidate_structure["structure_ok"])
    iv_env_label = str((iv_environment or {}).get("label") or "")

    if not structure_ok:
        reasons.append("Candidate structure is not ideal for a new PMCC short call")
    liquidity = candidate_structure.get("liquidity") or {}
    if not liquidity.get("ok"):
        reasons.append("Candidate failed liquidity hard filter: " + "; ".join((liquidity.get("reasons") or [])[:3]))

    if iv_rank is None:
        reasons.append("IV rank unavailable")
        if structure_ok and iv_env_label == "FAVORABLE":
            action = "CONSIDER_SELL"
            reasons.append("Composite IV environment is favorable despite missing IV rank")
        elif structure_ok and trend in {"DOWN", "FLAT"}:
            action = "CONSIDER_SELL"
            reasons.append("Trend and candidate structure are acceptable despite missing IV rank")
        elif structure_ok and trend == "UP":
            action = "CONSIDER_SELL"
            reasons.append("Trend is strong, so only consider a conservative short call without IV rank")
        else:
            reasons.append("Missing IV rank plus weak candidate structure argues for waiting")
    elif iv_rank >= config.iv_rank_sell_threshold and structure_ok:
        action = "SELL_CALL_WEAK" if trend == "UP" else "SELL_CALL"
        reasons.append("IV rank is elevated")
    elif iv_rank <= config.iv_rank_avoid_threshold:
        if structure_ok and iv_env_label == "FAVORABLE":
            action = "WAIT"
            reasons.append("IV rank is low but the composite IV environment offsets part of the concern")
        else:
            action = "AVOID_SELL"
            reasons.append("IV rank is low")
    elif not structure_ok:
        action = "WAIT"
        reasons.append("Wait for a cleaner DTE/OTM/delta candidate")
    elif iv_env_label == "FAVORABLE":
        action = "CONSIDER_SELL"
        reasons.append("IV rank is mid-range but the composite IV environment is favorable")
    else:
        action = "WAIT"
        reasons.append("IV rank is mid-range")

    if selected_delta is not None:
        reasons.append(f"Selected delta {selected_delta:.3f}")
    else:
        reasons.append("Greek data unavailable in this SDK")
    if selected_dte is not None:
        reasons.append(f"Selected expiry {selected_dte} DTE")
    if selected_otm_pct is not None:
        reasons.append(f"Strike is {selected_otm_pct * 100:.2f}% from spot")
    if selected_strike is not None and stock_price < selected_strike:
        reasons.append("Suggested strike is still out-of-the-money")

    return ActionDecision(
        action=action,
        reason=reasons,
        candidate_structure=candidate_structure,
    ).to_dict()
