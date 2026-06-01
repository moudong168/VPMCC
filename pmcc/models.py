from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StrategyConfig:
    leaps_strike: float = 140
    short_call_strike: Optional[float] = 230
    target_delta_low: float = 0.25
    target_delta_high: float = 0.40
    history_bars: int = 20
    preferred_dte_min: int = 7
    preferred_dte_max: int = 45
    iv_rank_sell_threshold: float = 70
    iv_rank_avoid_threshold: float = 30
    high_iv_roll_threshold: float = 70
    roll_delta_warn: float = 0.40
    roll_delta_danger: float = 0.50
    roll_delta_critical: float = 0.65
    roll_dte_attention: int = 21
    roll_dte_active: int = 14
    roll_dte_urgent: int = 7
    roll_profit_take: float = 50
    roll_profit_strong: float = 70
    iv_rank_override: Optional[float] = None
    iv_rank_overrides: Optional[Dict[str, float]] = None
    iv_percentile_override: Optional[float] = None
    iv_percentile_overrides: Optional[Dict[str, float]] = None
    iv_override: Optional[float] = None
    hv_override: Optional[float] = None
    trend_override: Optional[str] = None
    enable_web_validation: bool = True
    allow_event_short_call: bool = False
    earnings_block_days: int = 14
    event_block_days: int = 14
    ex_dividend_block_days: int = 7
    min_candidate_open_interest: int = 100
    min_candidate_volume: int = 10
    max_candidate_bid_ask_spread_pct: float = 0.18
    max_candidate_last_mid_deviation_pct: float = 0.35


@dataclass(frozen=True)
class PositionInput:
    raw_code: str
    underlying: str
    quantity: int
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_type: Optional[str] = None
    cost_price: Optional[float] = None


@dataclass(frozen=True)
class LiquidityAssessment:
    ok: bool
    reasons: List[str]
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    spread_pct: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]
    last_mid_deviation_pct: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "bid": self.bid,
            "ask": self.ask,
            "mid": round(self.mid, 4) if self.mid is not None else None,
            "spread": round(self.spread, 4) if self.spread is not None else None,
            "spread_pct": round(self.spread_pct * 100, 2) if self.spread_pct is not None else None,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "last_mid_deviation_pct": round(self.last_mid_deviation_pct * 100, 2) if self.last_mid_deviation_pct is not None else None,
        }


@dataclass(frozen=True)
class CandidateStructureAssessment:
    structure_ok: bool
    checks: Dict[str, bool]
    liquidity_ok: bool
    liquidity_reasons: List[str]
    bid_ask_spread_pct: Optional[float]
    last_mid_deviation_pct: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structure_ok": self.structure_ok,
            "checks": self.checks,
            "liquidity": {
                "ok": self.liquidity_ok,
                "reasons": self.liquidity_reasons,
                "bid_ask_spread_pct": self.bid_ask_spread_pct,
                "last_mid_deviation_pct": self.last_mid_deviation_pct,
                "volume": self.volume,
                "open_interest": self.open_interest,
            },
        }


@dataclass(frozen=True)
class ActionDecision:
    action: str
    reason: List[str]
    candidate_structure: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "candidate_structure": self.candidate_structure,
        }


@dataclass(frozen=True)
class IvEnvironmentAssessment:
    label: str
    score: int
    underlying_iv: Optional[float]
    hv: Optional[float]
    iv_hv_ratio: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    candidate_short_call_iv: Optional[float]
    average_long_leg_iv: Optional[float]
    short_vs_long_iv_spread: Optional[float]
    average_existing_slot_short_long_iv_spread: Optional[float]
    average_candidate_slot_short_long_iv_spread: Optional[float]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "score": self.score,
            "underlying_iv": self.underlying_iv,
            "hv": self.hv,
            "iv_hv_ratio": self.iv_hv_ratio,
            "iv_rank": self.iv_rank,
            "iv_percentile": self.iv_percentile,
            "candidate_short_call_iv": self.candidate_short_call_iv,
            "average_long_leg_iv": self.average_long_leg_iv,
            "short_vs_long_iv_spread": self.short_vs_long_iv_spread,
            "average_existing_slot_short_long_iv_spread": self.average_existing_slot_short_long_iv_spread,
            "average_candidate_slot_short_long_iv_spread": self.average_candidate_slot_short_long_iv_spread,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class EventRiskBlock:
    enabled: bool
    override_allowed: bool
    blocked: bool
    blocking_events: List[Dict[str, Any]]
    attention_events: List[Dict[str, Any]]
    event_calendar_file: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "override_allowed": self.override_allowed,
            "blocked": self.blocked,
            "blocking_events": self.blocking_events,
            "attention_events": self.attention_events,
            "event_calendar_file": self.event_calendar_file,
        }


@dataclass(frozen=True)
class ShortCallReview:
    code: str
    quantity: int
    strike: Optional[float]
    expiry: Optional[str]
    days_to_expiry: Optional[int]
    delta: Optional[float]
    abs_delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    iv: Optional[float]
    profit_capture_pct: Optional[float]
    price_to_strike_pct: Optional[float]
    iv_rank: Optional[float]
    roll_action: str
    rule_hits: List[str]
    reason: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "quantity": self.quantity,
            "strike": self.strike,
            "expiry": self.expiry,
            "days_to_expiry": self.days_to_expiry,
            "delta": self.delta,
            "abs_delta": round(self.abs_delta, 4) if self.abs_delta is not None else None,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "iv": self.iv,
            "profit_capture_pct": round(self.profit_capture_pct, 1) if self.profit_capture_pct is not None else None,
            "price_to_strike_pct": round(self.price_to_strike_pct, 2) if self.price_to_strike_pct is not None else None,
            "iv_rank": self.iv_rank,
            "roll_action": self.roll_action,
            "rule_hits": self.rule_hits,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ShortPutReview:
    code: str
    strategy_type: str
    quantity: int
    strike: Optional[float]
    expiry: Optional[str]
    days_to_expiry: Optional[int]
    delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    iv: Optional[float]
    bid_price: Optional[float]
    ask_price: Optional[float]
    last_price: Optional[float]
    mark_price: Optional[float]
    credit_received: Optional[float]
    profit_capture_pct: Optional[float]
    break_even: Optional[float]
    estimated_assignment_cost: Optional[float]
    max_loss_if_cash_secured: Optional[float]
    close_target_50pct: Optional[float]
    close_target_70pct: Optional[float]
    underlying_to_strike_pct: Optional[float]
    iv_rank: Optional[float]
    roll_action: str
    wheel_state: Dict[str, Any]
    payoff_scenarios: Dict[str, Any]
    operation_advice_text: str
    rule_hits: List[str]
    reason: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "strategy_type": self.strategy_type,
            "quantity": self.quantity,
            "strike": self.strike,
            "expiry": self.expiry,
            "days_to_expiry": self.days_to_expiry,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "iv": self.iv,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "last_price": self.last_price,
            "mark_price": round(self.mark_price, 3) if self.mark_price is not None else None,
            "credit_received": self.credit_received,
            "profit_capture_pct": round(self.profit_capture_pct, 1) if self.profit_capture_pct is not None else None,
            "break_even": round(self.break_even, 2) if self.break_even is not None else None,
            "estimated_assignment_cost": self.estimated_assignment_cost,
            "max_loss_if_cash_secured": round(self.max_loss_if_cash_secured, 2) if self.max_loss_if_cash_secured is not None else None,
            "close_target_50pct": self.close_target_50pct,
            "close_target_70pct": self.close_target_70pct,
            "underlying_to_strike_pct": round(self.underlying_to_strike_pct, 2) if self.underlying_to_strike_pct is not None else None,
            "iv_rank": self.iv_rank,
            "roll_action": self.roll_action,
            "wheel_state": self.wheel_state,
            "payoff_scenarios": self.payoff_scenarios,
            "operation_advice_text": self.operation_advice_text,
            "rule_hits": self.rule_hits,
            "reason": self.reason,
        }
