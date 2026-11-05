import math
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from pmcc.constants import OPTION_CHAIN_MIN_INTERVAL_SECONDS
from pmcc.models import PositionInput, StrategyConfig
from pmcc.positions import parse_option_code_metadata
from pmcc.utils import parse_expiry, safe_float, safe_text

LAST_OPTION_CHAIN_REQUEST_AT = 0.0

# Safety cap for the paginated Futu daily-IV series (one page is a full year by default).
MAX_HIS_VOLATILITY_PAGES = 10


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
        except Exception as exc:
            errors.append(f"{kwargs}: {type(exc).__name__}({exc})")
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
        except Exception as exc:
            errors.append(f"{kwargs}: {type(exc).__name__}({exc})")
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

    # Chunk-level errors (non-fatal when other chunks succeeded)
    for err in errors:
        print(f"WARNING: get_greeks chunk failure: {err}")

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
    for target in field_map.values():
        greeks[target] = float("nan")
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
    except Exception as exc:
        print(f"WARNING: request_history_kline failed for {symbol}: {exc}")
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


def fetch_underlying_daily_iv_series(symbol: str, ctx: Any, days: int = 360) -> Dict[str, Any]:
    """Fetch Futu OpenD's daily implied-volatility series for an underlying.

    Calls ``OpenQuoteContext.get_option_underlying_his_volatility`` and follows
    ``page_req_key`` until it is exhausted (capped at ``MAX_HIS_VOLATILITY_PAGES``).
    The official signature is
    ``get_option_underlying_his_volatility(code, index_option_type=IndexOptionType.NORMAL,
    begin_time=None, end_time=None, page_req_key=None)``, so every argument after
    ``code`` MUST be passed by keyword - a positional call silently lands the
    begin-date string in ``index_option_type`` and the request fails. The requested
    span is capped at 364 days, the official maximum for this endpoint.
    The ``iv`` column is already in percent, i.e. the same unit as the locally
    accumulated PMCC IV history, so no scaling is applied anywhere.

    Returns ``{"values": [...], "start_date": ..., "end_date": ...}`` with values
    in chronological order. This helper is strictly best-effort and never raises:
    any failure (no context, non-OK return code, missing columns, empty payload,
    exception) yields an empty ``values`` list.
    """
    result: Dict[str, Any] = {
        "values": [],
        "start_date": None,
        "end_date": None,
        "rows": 0,
        "pages": 0,
        "error": None,
    }
    if ctx is None:
        result["error"] = "missing_quote_context"
        return result

    try:
        from futu import RET_OK

        if not hasattr(ctx, "get_option_underlying_his_volatility"):
            result["error"] = "unsupported_quote_context"
            return result

        end_day = date.today()
        # Official maximum span for this endpoint is 364 days; keep the request inside it.
        span_days = min(max(int(days), 20), 364)
        begin_day = end_day - timedelta(days=span_days)
        begin_time = begin_day.isoformat()
        end_time = end_day.isoformat()

        frames: List[pd.DataFrame] = []
        page_req_key = None
        for _ in range(MAX_HIS_VOLATILITY_PAGES):
            ret, data, page_req_key = ctx.get_option_underlying_his_volatility(
                symbol,
                begin_time=begin_time,
                end_time=end_time,
                page_req_key=page_req_key,
            )
            result["pages"] += 1
            if ret != RET_OK:
                if not frames:
                    result["error"] = format_futu_error(ret, data)
                    return result
                break
            if isinstance(data, pd.DataFrame) and not data.empty:
                frames.append(data.copy())
            if not page_req_key:
                break

        if not frames:
            return result

        frame = pd.concat(frames, ignore_index=True)
        if "iv" not in frame.columns:
            result["error"] = "missing_iv_column"
            return result

        working = frame.copy()
        working["iv_numeric"] = pd.to_numeric(working["iv"], errors="coerce")
        finite_mask = [
            bool(value is not None and not pd.isna(value) and math.isfinite(float(value)))
            for value in working["iv_numeric"].tolist()
        ]
        working = working[finite_mask].copy()
        if working.empty:
            return result

        if "time" in working.columns:
            working["series_time"] = pd.to_datetime(working["time"], errors="coerce")
            working = working.sort_values(by=["series_time"], kind="mergesort", na_position="last")
            known_dates = [value.date().isoformat() for value in working["series_time"].tolist() if not pd.isna(value)]
        else:
            known_dates = []

        values = [float(value) for value in working["iv_numeric"].tolist()]
        result["values"] = values
        result["rows"] = len(values)
        result["start_date"] = known_dates[0] if known_dates else None
        result["end_date"] = known_dates[-1] if known_dates else None
        return result
    except Exception as exc:
        result["values"] = []
        result["start_date"] = None
        result["end_date"] = None
        result["rows"] = 0
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def fetch_underlying_iv_overview(symbol: str, ctx: Any) -> Dict[str, Any]:
    """Fetch Futu OpenD's direct IV overview for an underlying.

    Calls ``OpenQuoteContext.get_option_underlying_overview([symbol])``, which
    returns ``(ret, data)`` where ``data`` is a DataFrame with columns such as
    ``code``, ``name``, ``iv``, ``iv_rank``, ``iv_percentile``, ``pre_iv``,
    ``hv_30d``. All values are already in PERCENT - the same numbers the Futu app
    shows in its volatility analysis panel - so this helper never rescales them.

    Returns ``{"iv": float|None, "iv_rank": float|None,
    "iv_percentile": float|None, "error": str|None}``. This helper is strictly
    best-effort and never raises: any failure (missing context, unsupported
    context, non-OK return code, empty payload, missing/non-finite ``iv_rank``,
    exception) yields ``iv_rank`` ``None`` plus a non-empty ``error`` string.
    """
    result: Dict[str, Any] = {
        "iv": None,
        "iv_rank": None,
        "iv_percentile": None,
        "error": None,
    }
    if ctx is None:
        result["error"] = "missing_quote_context"
        return result

    try:
        from futu import RET_OK

        if not hasattr(ctx, "get_option_underlying_overview"):
            result["error"] = "unsupported_quote_context"
            return result

        ret, data = ctx.get_option_underlying_overview([symbol])
        if ret != RET_OK:
            result["error"] = format_futu_error(ret, data)
            return result
        if not isinstance(data, pd.DataFrame) or data.empty:
            result["error"] = "empty_overview_data"
            return result

        row = None
        if "code" in data.columns:
            matched = data[data["code"] == symbol]
            if not matched.empty:
                row = matched.iloc[0]
        if row is None:
            row = data.iloc[0]

        def finite_field(field: str) -> Optional[float]:
            if field not in row.index:
                return None
            try:
                value = float(row[field])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            return value

        result["iv"] = finite_field("iv")
        result["iv_rank"] = finite_field("iv_rank")
        result["iv_percentile"] = finite_field("iv_percentile")
        if "iv_rank" not in row.index:
            result["error"] = "missing_iv_rank_column"
        elif result["iv_rank"] is None:
            result["error"] = "non_finite_iv_rank"
        return result
    except Exception as exc:
        result["iv"] = None
        result["iv_rank"] = None
        result["iv_percentile"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


# Official hard limit of get_earnings_calendar: begin_date/end_date may span at most 7 days.
OPEND_EARNINGS_MAX_WINDOW_DAYS = 7
# The endpoint is quote-only; only OpenQuoteContext is used here (never a trade context).
OPEND_EARNINGS_SECURITY_COLUMNS = ("security", "stock_code", "code")
OPEND_EARNINGS_DATE_COLUMNS = ("earnings_date", "date")
OPEND_EARNINGS_PUB_TYPE_MAP = {
    "BEFORE": "before_market",
    "AFTER": "after_market",
    "REGULAR": "intraday",
}


def earnings_symbol_forms(symbol: str) -> set:
    """Return the accepted security spellings for one symbol (``US.NVDA`` / ``NVDA``)."""
    text = (safe_text(symbol) or "").upper()
    if not text:
        return set()
    if "." in text:
        return {text, text.split(".")[-1]}
    return {text, f"US.{text}"}


def pick_opend_earnings_row(data: Any, symbol: str, today: date) -> Optional[Tuple[date, Any]]:
    """Return the nearest ``(earnings_date, row)`` for *symbol* with date >= *today*."""
    if not isinstance(data, pd.DataFrame) or data.empty:
        return None
    security_column = next((column for column in OPEND_EARNINGS_SECURITY_COLUMNS if column in data.columns), None)
    date_column = next((column for column in OPEND_EARNINGS_DATE_COLUMNS if column in data.columns), None)
    if security_column is None or date_column is None:
        return None
    forms = earnings_symbol_forms(symbol)
    best: Optional[Tuple[date, Any]] = None
    for _, row in data.iterrows():
        security = safe_text(row.get(security_column))
        if security is None or security.upper() not in forms:
            continue
        parsed = parse_expiry(row.get(date_column))
        if parsed is None or parsed < today:
            continue
        if best is None or parsed < best[0]:
            best = (parsed, row)
    return best


def fetch_opend_earnings_calendar(symbol: str, ctx: Any, horizon_days: int = 98) -> Dict[str, Any]:
    """Fetch the next earnings date from Futu OpenD's quote-only earnings calendar.

    Calls the official v10.12 signature
    ``get_earnings_calendar(market, sort_type=None, begin_date=None, end_date=None, filter_list=None)``
    with KEYWORD arguments only (a positional call lands the market in the wrong slot and
    fails silently). Because OpenD caps a single request at a 7-day window, the horizon is
    scanned in consecutive windows where ``end_date - begin_date`` never exceeds
    ``OPEND_EARNINGS_MAX_WINDOW_DAYS``; the scan stops when the horizon is covered or a
    matching row is found. Rows are filtered locally on the security column, accepting both
    ``US.NVDA`` and bare ``NVDA``, and the nearest date >= today wins.

    This helper never raises and never swallows a failure: RET_ERROR, a malformed payload and
    any exception all come back as ``status='ERROR'`` with the error text in ``error``.
    """
    record: Dict[str, Any] = {
        "name": "Futu OpenD Earnings Calendar",
        "source": "futu_opend_earnings",
        "status": "ERROR",
        "tier": "primary",
    }
    if ctx is None:
        record["error"] = "missing_quote_context"
        return record

    try:
        from futu import RET_OK, Market
    except Exception as exc:  # pragma: no cover - only when futu is not importable
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    today = date.today()
    windows_scanned = 0
    try:
        horizon = max(int(horizon_days), 1)
        record["horizon_days"] = horizon
        latest_day = today + timedelta(days=horizon - 1)
        window_start = today
        for _ in range(horizon // OPEND_EARNINGS_MAX_WINDOW_DAYS + 2):
            if window_start > latest_day:
                break
            window_end = min(
                window_start + timedelta(days=OPEND_EARNINGS_MAX_WINDOW_DAYS - 1),
                latest_day,
            )
            ret, data = ctx.get_earnings_calendar(
                market=Market.US,
                begin_date=window_start.isoformat(),
                end_date=window_end.isoformat(),
            )
            windows_scanned += 1
            if ret != RET_OK:
                record["windows_scanned"] = windows_scanned
                record["error"] = format_futu_error(ret, data)
                return record
            found = pick_opend_earnings_row(data, symbol, today)
            if found is not None:
                found_date, row = found
                pub_type = safe_text(row.get("pub_type")) if "pub_type" in getattr(row, "index", []) else None
                mapped_time = OPEND_EARNINGS_PUB_TYPE_MAP.get((pub_type or "").upper())
                record.update(
                    {
                        "status": "OK",
                        "ticker": (safe_text(symbol) or "").upper(),
                        "next_earnings_date": found_date.isoformat(),
                        "days_to_earnings": (found_date - today).days,
                        "time_of_day": mapped_time,
                        "pub_type": pub_type,
                        "confirmation_status": "opend",
                        "windows_scanned": windows_scanned,
                        "window_begin": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                    }
                )
                return record
            window_start = window_end + timedelta(days=1)
        record["windows_scanned"] = windows_scanned
        record["error"] = f"no earnings date for {symbol} within {record['horizon_days']} days"
        return record
    except Exception as exc:
        record["windows_scanned"] = windows_scanned
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record


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
            print(f"WARNING: position_side missing for {code} (qty={raw_quantity}), defaulting to SHORT. Verify manually.")
            position_side_upper = "SHORT"
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
