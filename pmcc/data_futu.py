import math
import time
from typing import Any, List, Optional, Tuple

import pandas as pd

from pmcc.constants import OPTION_CHAIN_MIN_INTERVAL_SECONDS
from pmcc.models import PositionInput, StrategyConfig
from pmcc.positions import parse_option_code_metadata
from pmcc.utils import parse_expiry, safe_float, safe_text

LAST_OPTION_CHAIN_REQUEST_AT = 0.0


def format_futu_error(ret: Any, data: Any) -> str:
    if isinstance(data, pd.DataFrame):
        detail = "empty DataFrame" if data.empty else repr(data.head(3).to_dict(orient="records"))
    elif isinstance(data, pd.Series):
        detail = repr(data.to_dict())
    else:
        detail = str(data)
    return f"ret={ret}, detail={detail}"


def enrich_error_message(message: str) -> str:
    lowered = message.lower()
    if "https://" in message and (
        "问卷" in message
        or "协议" in message
        or "questionnaire" in lowered
        or "agreement" in lowered
    ):
        return message + " Hint: complete the required Futu questionnaire/agreement first."
    if "无权限" in message or "权限" in message or "permission" in lowered:
        return message + " Hint: check the US market quote permission on the Futu account connected to OpenD."
    return message


def normalize_option_chain(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    chain = data.copy()
    if "strike_price" in chain.columns:
        chain["strike_price"] = pd.to_numeric(chain["strike_price"], errors="coerce")
    return chain.sort_values(by=["strike_price"]).reset_index(drop=True)


def get_quote(symbol: str, ctx: Any) -> Tuple[pd.Series, str]:
    from futu import RET_OK

    ret, data = ctx.get_stock_quote([symbol])
    if ret == RET_OK and data is not None and not data.empty:
        return data.iloc[0], "stock_quote"

    ret, data = ctx.get_market_snapshot([symbol])
    if ret == RET_OK and data is not None and not data.empty:
        return data.iloc[0], "market_snapshot"

    raise RuntimeError(enrich_error_message(f"Failed to get quote for {symbol}: {format_futu_error(ret, data)}"))


def get_option_expiries(symbol: str, ctx: Any) -> pd.DataFrame:
    from futu import RET_OK

    if not hasattr(ctx, "get_option_expiration_date"):
        return pd.DataFrame()
    ret, data = ctx.get_option_expiration_date(symbol)
    if ret == RET_OK and data is not None and not data.empty:
        return data.copy()
    return pd.DataFrame()


def get_preferred_expiry_dates(symbol: str, ctx: Any, config: Optional[StrategyConfig] = None) -> List[str]:
    config = config or StrategyConfig()
    expiries = get_option_expiries(symbol, ctx)
    if expiries.empty or "strike_time" not in expiries.columns:
        return []
    working = expiries.copy()
    if "option_expiry_date_distance" in working.columns:
        working["dte"] = pd.to_numeric(working["option_expiry_date_distance"], errors="coerce")
    else:
        working["expiry_date"] = working["strike_time"].apply(parse_expiry)
        today = pd.Timestamp.today().date()
        working["dte"] = working["expiry_date"].apply(lambda value: (value - today).days if value else None)

    preferred = working[
        working["dte"].between(config.preferred_dte_min, config.preferred_dte_max, inclusive="both")
    ].sort_values(by=["dte"])
    if preferred.empty:
        preferred = working[pd.to_numeric(working["dte"], errors="coerce") >= 0].sort_values(by=["dte"]).head(6)
    return [str(value) for value in preferred["strike_time"].dropna().tolist()]


def throttle_option_chain_request() -> None:
    global LAST_OPTION_CHAIN_REQUEST_AT

    now = time.monotonic()
    elapsed = now - LAST_OPTION_CHAIN_REQUEST_AT
    if LAST_OPTION_CHAIN_REQUEST_AT > 0 and elapsed < OPTION_CHAIN_MIN_INTERVAL_SECONDS:
        time.sleep(OPTION_CHAIN_MIN_INTERVAL_SECONDS - elapsed)
    LAST_OPTION_CHAIN_REQUEST_AT = time.monotonic()


def get_option_chain(symbol: str, ctx: Any, config: Optional[StrategyConfig] = None) -> pd.DataFrame:
    from futu import OptionType, RET_OK

    config = config or StrategyConfig()
    preferred_dates = get_preferred_expiry_dates(symbol, ctx, config)
    errors: List[str] = []

    chains: List[pd.DataFrame] = []
    for expiry in preferred_dates:
        kwargs = {"code": symbol, "start": expiry, "end": expiry, "option_type": OptionType.CALL}
        try:
            throttle_option_chain_request()
            ret, data = ctx.get_option_chain(**kwargs)
        except TypeError as exc:
            errors.append(f"{kwargs}: TypeError({exc})")
            continue
        if ret == RET_OK and data is not None and not data.empty:
            chains.append(normalize_option_chain(data))
            continue
        errors.append(f"{kwargs}: {format_futu_error(ret, data)}")

    if chains:
        return normalize_option_chain(pd.concat(chains, ignore_index=True).drop_duplicates(subset=["code"]))

    attempts = [
        {"code": symbol, "option_type": OptionType.CALL},
        {"code": symbol, "start": None, "end": None, "option_type": OptionType.CALL},
    ]
    for kwargs in attempts:
        try:
            throttle_option_chain_request()
            ret, data = ctx.get_option_chain(**kwargs)
        except TypeError as exc:
            errors.append(f"{kwargs}: TypeError({exc})")
            continue
        if ret == RET_OK and data is not None and not data.empty:
            return normalize_option_chain(data)
        errors.append(f"{kwargs}: {format_futu_error(ret, data)}")

    raise RuntimeError(enrich_error_message(f"Failed to get call option chain for {symbol}: {' | '.join(errors)}"))


def get_greeks(codes: List[str], ctx: Any) -> pd.DataFrame:
    from futu import RET_OK

    if not codes:
        return pd.DataFrame(columns=["code"])

    snapshots: List[pd.DataFrame] = []
    errors: List[str] = []
    chunk_size = 200
    for start in range(0, len(codes), chunk_size):
        chunk = codes[start:start + chunk_size]
        ret, data = ctx.get_market_snapshot(chunk)
        if ret == RET_OK and data is not None and not data.empty:
            snapshots.append(data.copy())
        else:
            errors.append(format_futu_error(ret, data))

    if not snapshots:
        if hasattr(ctx, "get_option_greeks"):
            ret, data = ctx.get_option_greeks(codes)
            if ret == RET_OK and data is not None and not data.empty:
                return data.copy()
            errors.append(format_futu_error(ret, data))
        raise RuntimeError(enrich_error_message(f"Failed to get option greeks from market snapshot: {' | '.join(errors)}"))

    snapshot = pd.concat(snapshots, ignore_index=True)
    greeks = pd.DataFrame({"code": snapshot["code"]})
    field_map = {
        "option_delta": "delta",
        "option_gamma": "gamma",
        "option_vega": "vega",
        "option_theta": "theta",
        "option_rho": "rho",
        "option_implied_volatility": "implied_volatility",
        "option_premium": "premium",
        "option_open_interest": "open_interest",
        "bid_price": "bid_price",
        "ask_price": "ask_price",
        "last_price": "last_price",
        "volume": "volume",
    }
    for source, target in field_map.items():
        if source in snapshot.columns:
            greeks[target] = snapshot[source]

    return greeks


def get_daily_klines(symbol: str, ctx: Any, bars: int) -> pd.DataFrame:
    from futu import KLType, RET_OK, SubType

    ret, data = ctx.get_cur_kline(symbol, bars, KLType.K_DAY)
    if ret == RET_OK and data is not None and not data.empty:
        return data.copy()

    ret, _ = ctx.subscribe([symbol], [SubType.K_DAY], subscribe_push=False)
    if ret == RET_OK:
        ret, data = ctx.get_cur_kline(symbol, bars, KLType.K_DAY)
        if ret == RET_OK and data is not None and not data.empty:
            return data.copy()

    try:
        ret, data, _ = ctx.request_history_kline(symbol, ktype=KLType.K_DAY, max_count=max(bars, 60))
    except Exception:
        return pd.DataFrame()
    if ret == RET_OK and data is not None and not data.empty:
        return data.copy()
    return pd.DataFrame()


def get_trend(symbol: str, ctx: Any, bars: int) -> str:
    data = get_daily_klines(symbol, ctx, max(bars, 20))
    if data.empty or "close" not in data.columns:
        return "UNKNOWN"
    closes = pd.to_numeric(data["close"], errors="coerce").dropna()
    if len(closes) < 10:
        return "UNKNOWN"

    ma_short = closes.tail(5).mean()
    ma_medium = closes.tail(min(20, len(closes))).mean()
    latest = closes.iloc[-1]
    previous = closes.iloc[-6] if len(closes) >= 6 else closes.iloc[0]
    recent_return = (latest / previous - 1) if previous else 0.0
    ma_gap = (ma_short / ma_medium - 1) if ma_medium else 0.0

    if ma_gap > 0.003 and recent_return > 0.005:
        return "UP"
    if ma_gap < -0.003 and recent_return < -0.005:
        return "DOWN"
    return "FLAT"


def estimate_historical_volatility(symbol: str, ctx: Any, bars: int = 31) -> Optional[float]:
    data = get_daily_klines(symbol, ctx, bars)
    if data.empty or "close" not in data.columns or len(data) < 8:
        return None
    closes = pd.to_numeric(data["close"], errors="coerce").dropna()
    if len(closes) < 8:
        return None
    ratios = (closes / closes.shift(1)).dropna()
    log_returns = pd.Series([math.log(value) for value in ratios if value > 0])
    if len(log_returns) < 7:
        return None
    return round(float(log_returns.std(ddof=1) * (252 ** 0.5) * 100), 2)


def collect_positions_from_opend(
    host: str,
    port: int,
    trd_env: Any = None,
    acc_index: Optional[int] = None,
) -> Tuple[List[PositionInput], List[PositionInput], dict]:
    from futu import OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket

    if trd_env is None:
        trd_env = TrdEnv.REAL
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port)
    account_position_counts: List[dict] = []
    query_errors: List[str] = []
    accounts = None
    account_indices: List[int] = []
    try:
        ret, accounts = ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(enrich_error_message(f"Failed to get account list: {format_futu_error(ret, accounts)}"))

        account_count = 0 if accounts is None else len(accounts)
        account_indices = [acc_index] if acc_index is not None else list(range(account_count or 1))
        position_frames: List[pd.DataFrame] = []
        for current_acc_index in account_indices:
            ret, positions = ctx.position_list_query(trd_env=trd_env, acc_index=current_acc_index, refresh_cache=True)
            if ret != RET_OK:
                query_errors.append(f"acc_index={current_acc_index}: {format_futu_error(ret, positions)}")
                continue
            row_count = 0 if positions is None else int(len(positions))
            account_position_counts.append({"acc_index": current_acc_index, "positions_seen": row_count})
            if positions is not None and not positions.empty:
                frame = positions.copy()
                frame["opend_acc_index"] = current_acc_index
                position_frames.append(frame)

        if not position_frames and query_errors:
            raise RuntimeError(enrich_error_message(f"Failed to get positions from all accounts: {' | '.join(query_errors)}"))
        positions = pd.concat(position_frames, ignore_index=True) if position_frames else pd.DataFrame()
    finally:
        ctx.close()

    base_positions: List[PositionInput] = []
    short_calls: List[PositionInput] = []
    skipped: List[dict] = []

    if positions is None or positions.empty:
        return [], [], {
            "source": "opend",
            "account_count": 0 if accounts is None else len(accounts),
            "queried_account_indices": account_indices,
            "account_position_counts": account_position_counts,
            "query_errors": query_errors,
            "skipped": skipped,
        }

    for _, row in positions.iterrows():
        code = safe_text(row.get("code"))
        raw_quantity = safe_float(row.get("qty"))
        if code is None or raw_quantity is None or raw_quantity == 0:
            continue
        quantity = int(round(abs(raw_quantity)))
        if quantity <= 0:
            continue

        meta = parse_option_code_metadata(code)
        option_type = meta["option_type"]
        if option_type not in {"CALL", "PUT"}:
            skipped.append({"code": code, "qty": raw_quantity, "reason": "unsupported_option_code"})
            continue

        position_side = safe_text(row.get("position_side")) or ""
        position_side_upper = position_side.upper()
        if not position_side_upper:
            position_side_upper = "SHORT" if raw_quantity < 0 else "LONG"
        cost_price = safe_float(row.get("average_cost"))
        if cost_price is None:
            cost_price = safe_float(row.get("cost_price"))

        position = PositionInput(
            raw_code=code,
            underlying=meta["underlying"],
            quantity=quantity,
            strike=meta["strike"],
            expiry=meta["expiry"],
            option_type=meta["option_type"],
            cost_price=cost_price,
        )

        if "SHORT" in position_side_upper:
            short_calls.append(position)
        elif "LONG" in position_side_upper and option_type == "CALL":
            base_positions.append(position)
        elif "LONG" in position_side_upper:
            skipped.append({"code": code, "qty": raw_quantity, "position_side": position_side, "reason": f"long_{option_type.lower()}_not_pmcc_base"})
        else:
            skipped.append({"code": code, "qty": raw_quantity, "position_side": position_side, "reason": f"unknown_position_side:{position_side}"})

    short_call_positions = [item for item in short_calls if item.option_type == "CALL"]
    short_put_positions = [item for item in short_calls if item.option_type == "PUT"]
    metadata = {
        "source": "opend",
        "account_count": 0 if accounts is None else len(accounts),
        "queried_account_indices": account_indices,
        "account_position_counts": account_position_counts,
        "query_errors": query_errors,
        "positions_seen": int(len(positions)),
        "base_contracts": sum(item.quantity for item in base_positions),
        "short_contracts": sum(item.quantity for item in short_calls),
        "short_call_contracts": sum(item.quantity for item in short_call_positions),
        "short_put_contracts": sum(item.quantity for item in short_put_positions),
        "skipped": skipped,
    }
    return base_positions, short_calls, metadata
