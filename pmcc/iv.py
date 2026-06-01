from typing import Any, Dict, List, Optional

from pmcc.models import IvEnvironmentAssessment
from pmcc.utils import safe_float


def build_iv_environment(
    underlying_iv: Optional[float],
    hv: Optional[float],
    iv_rank: Optional[float],
    iv_percentile: Optional[float],
    candidate_option: Optional[Any] = None,
    long_leg_analysis: Optional[Dict[str, Any]] = None,
    slot_iv_spread_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    candidate_iv = None
    if candidate_option is not None:
        candidate_iv = safe_float(candidate_option.get("implied_volatility")) or safe_float(candidate_option.get("iv"))

    long_leg_ivs: List[float] = []
    if long_leg_analysis:
        for leg in long_leg_analysis.get("legs") or []:
            value = safe_float(leg.get("iv"))
            if value is not None:
                long_leg_ivs.append(value)
    avg_long_iv = round(sum(long_leg_ivs) / len(long_leg_ivs), 4) if long_leg_ivs else None

    iv_hv_ratio = None
    if underlying_iv is not None and hv is not None and hv > 0:
        iv_hv_ratio = round(underlying_iv / hv, 3)

    term_spread = None
    if candidate_iv is not None and avg_long_iv is not None:
        term_spread = round(candidate_iv - avg_long_iv, 4)
    average_existing_slot_spread = None
    average_candidate_slot_spread = None
    if slot_iv_spread_analysis:
        average_existing_slot_spread = safe_float(slot_iv_spread_analysis.get("average_existing_short_long_iv_spread"))
        average_candidate_slot_spread = safe_float(slot_iv_spread_analysis.get("average_candidate_short_long_iv_spread"))

    score = 0
    notes: List[str] = []
    if iv_rank is not None:
        if iv_rank >= 70:
            score += 2
            notes.append("IV Rank is high")
        elif iv_rank <= 30:
            score -= 2
            notes.append("IV Rank is low")
        else:
            notes.append("IV Rank is mid-range")
    else:
        notes.append("IV Rank unavailable")

    if iv_percentile is not None:
        if iv_percentile >= 60:
            score += 1
            notes.append("IV Percentile supports richer option premium")
        elif iv_percentile <= 30:
            score -= 1
            notes.append("IV Percentile is low")

    if iv_hv_ratio is not None:
        if iv_hv_ratio >= 1.10:
            score += 1
            notes.append("IV is above recent realized volatility")
        elif iv_hv_ratio < 0.90:
            score -= 1
            notes.append("IV is below recent realized volatility")

    spread_for_score = average_candidate_slot_spread
    if spread_for_score is None:
        spread_for_score = average_existing_slot_spread
    if spread_for_score is None:
        spread_for_score = term_spread

    if spread_for_score is not None:
        if spread_for_score >= 0:
            score += 1
            notes.append("slot-level short-call IV is not below paired long-leg IV")
        else:
            score -= 1
            notes.append("slot-level short-call IV is below paired long-leg IV")

    if score >= 2:
        label = "FAVORABLE"
    elif score <= -2:
        label = "UNFAVORABLE"
    else:
        label = "NEUTRAL"

    return IvEnvironmentAssessment(
        label=label,
        score=score,
        underlying_iv=underlying_iv,
        hv=hv,
        iv_hv_ratio=iv_hv_ratio,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        candidate_short_call_iv=candidate_iv,
        average_long_leg_iv=avg_long_iv,
        short_vs_long_iv_spread=term_spread,
        average_existing_slot_short_long_iv_spread=average_existing_slot_spread,
        average_candidate_slot_short_long_iv_spread=average_candidate_slot_spread,
        notes=notes,
    ).to_dict()
