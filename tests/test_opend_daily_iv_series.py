"""Tests for the Futu OpenD daily IV series resolver priority.

Covers ``pmcc.data_futu.fetch_underlying_daily_iv_series`` and the new
``opend_daily_iv_series`` branch of ``build_iv_rank_analysis`` in
futu_option_decision.py:

(a) a two-page series is fully consumed (page_req_key followed) and sorted,
(b) a good series wins over local history with hand-computed rank/percentile,
(c) a series shorter than 20 samples falls through to local history / unavailable,
(d) a quote context that raises falls through without propagating the error,
(e) no context keeps today's behaviour (local history, memory, unavailable),
(f) the API ``iv`` column is already in percent - no scaling anywhere.
"""

import contextlib
import json
import math
import re
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

SYMBOL = "US.MSFT"

# Official OpenD signature of the endpoint under test:
#   get_option_underlying_his_volatility(code, index_option_type=IndexOptionType.NORMAL,
#                                       begin_time=None, end_time=None, page_req_key=None)
# A positional begin-date lands in ``index_option_type``; these guards make that
# regression impossible to miss again.
DATE_LIKE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_OFFICIAL_SPAN_DAYS = 364


def validate_his_volatility_call(index_option_type, begin_time, end_time) -> None:
    if isinstance(index_option_type, str) and DATE_LIKE_PATTERN.match(index_option_type):
        raise TypeError(
            "get_option_underlying_his_volatility() got a date-like string as "
            f"index_option_type={index_option_type!r}; begin_time/end_time must be keyword arguments"
        )
    if begin_time and end_time:
        span_days = (date.fromisoformat(str(end_time)) - date.fromisoformat(str(begin_time))).days
        if span_days > MAX_OFFICIAL_SPAN_DAYS:
            raise AssertionError(
                f"requested span {span_days}d exceeds the official maximum of "
                f"{MAX_OFFICIAL_SPAN_DAYS}d"
            )


def install_futu_stub() -> None:
    futu_stub = types.ModuleType("futu")
    futu_stub.OpenQuoteContext = type("OpenQuoteContext", (), {})
    futu_stub.OptionType = type("OptionType", (), {"CALL": "CALL", "PUT": "PUT"})
    futu_stub.RET_OK = 0
    futu_stub.TrdEnv = type("TrdEnv", (), {"REAL": "REAL"})
    sys.modules["futu"] = futu_stub


# The IV-series helper calls ``from futu import RET_OK`` lazily, so a stub module
# must exist even when futu_option_decision imported fine through its own
# ImportError fallback.
if "futu" not in sys.modules:
    try:
        import futu  # noqa: F401  (installed futu-api)
    except ModuleNotFoundError:
        install_futu_stub()

import futu_option_decision as pmcc
from pmcc import data_futu as pmcc_data_futu


def build_series_frame(values, start_day: date = date(2025, 1, 2)) -> pd.DataFrame:
    """Build a DataFrame with the documented get_option_underlying_his_volatility shape."""
    rows = []
    for offset, value in enumerate(values):
        day = start_day + timedelta(days=offset)
        rows.append(
            {
                "code": SYMBOL,
                "name": "MSFT",
                "time": day.isoformat(),
                "timestamp": 1700000000 + offset * 86400,
                "iv": value,
                "hv": round(value - 2.0, 4),
                "underlying_price": 400.0,
            }
        )
    return pd.DataFrame(rows)


class FakeQuoteContext:
    """Returns the given pages one by one, following page_req_key pagination."""

    def __init__(self, pages) -> None:
        self.pages = list(pages)
        self.calls = []

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        self.calls.append(
            {
                "code": code,
                "begin_time": begin_time,
                "end_time": end_time,
                "page_req_key": page_req_key,
            }
        )
        if page_req_key is None:
            index = 0
        else:
            index = int(page_req_key)
        if index >= len(self.pages):
            return 0, pd.DataFrame(), None
        next_key = str(index + 1) if index + 1 < len(self.pages) else None
        return 0, self.pages[index].copy(), next_key


class RaisingQuoteContext:
    def __init__(self, fail_on_page: int = 0) -> None:
        self.fail_on_page = fail_on_page
        self.calls = 0

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        self.calls += 1
        if self.calls > self.fail_on_page:
            raise RuntimeError("OpenD connection dropped")
        return 0, build_series_frame([30.0] * 25), "next-page"


class NoVolatilityQuoteContext:
    """Quote context without the his-volatility endpoint (older futu-api)."""


class ErrorReturnQuoteContext:
    def __init__(self, message: str = "no permission") -> None:
        self.message = message

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        return -1, self.message, None


class MissingIvColumnQuoteContext:
    def __init__(self) -> None:
        self.frame = pd.DataFrame(
            [
                {"code": SYMBOL, "name": "MSFT", "time": "2025-01-02", "hv": 25.0, "underlying_price": 400.0},
            ]
        )

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        return 0, self.frame.copy(), None


def build_local_history_payload(values) -> dict:
    start = date(2024, 6, 3)
    history = [
        {"date": (start + timedelta(days=offset)).isoformat(), "iv": value}
        for offset, value in enumerate(values)
    ]
    return {"schema_version": 1, "records": {SYMBOL: {"history": history}}}


class OpendDailyIvSeriesHelperTests(unittest.TestCase):
    def test_signature_guards_reject_positional_date_and_oversized_span(self) -> None:
        # Pins the 2026-11-04 bug: a positional begin-date landed in
        # index_option_type. The fake now raises TypeError for that shape.
        ctx = FakeQuoteContext([build_series_frame([30.0] * 3)])
        with self.assertRaises(TypeError):
            ctx.get_option_underlying_his_volatility(SYMBOL, "2025-01-02", "2025-12-31")
        # The official endpoint rejects spans longer than 364 days.
        with self.assertRaises(AssertionError):
            ctx.get_option_underlying_his_volatility(
                SYMBOL, begin_time="2024-01-01", end_time="2025-12-31"
            )
        # 360 days (the new default) is inside the official maximum.
        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, FakeQuoteContext([build_series_frame([30.0] * 3)]))
        self.assertIsNone(result["error"])

    def test_two_page_series_is_fully_consumed_and_sorted(self) -> None:
        later_values = [31.0, 32.0, 33.0, 34.0]
        earlier_values = [27.0, 28.0, 29.0, 30.0]
        ctx = FakeQuoteContext(
            [
                build_series_frame(later_values, start_day=date(2025, 3, 3)),
                build_series_frame(earlier_values, start_day=date(2025, 2, 3)),
            ]
        )

        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)

        self.assertEqual(len(ctx.calls), 2)
        self.assertIsNone(ctx.calls[0]["page_req_key"])
        self.assertEqual(ctx.calls[1]["page_req_key"], "1")
        self.assertRegex(ctx.calls[0]["begin_time"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(ctx.calls[0]["end_time"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertLess(ctx.calls[0]["begin_time"], ctx.calls[0]["end_time"])
        self.assertEqual(ctx.calls[0]["code"], SYMBOL)

        self.assertEqual(result["values"], earlier_values + later_values)
        self.assertEqual(result["rows"], 8)
        self.assertEqual(result["pages"], 2)
        self.assertIsNone(result["error"])
        self.assertEqual(result["start_date"], "2025-02-03")
        self.assertEqual(result["end_date"], "2025-03-06")

    def test_pagination_stops_at_page_cap(self) -> None:
        frame = build_series_frame([30.0] * 3)
        ctx = FakeQuoteContext([frame.copy() for _ in range(20)])

        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)

        self.assertEqual(result["pages"], pmcc_data_futu.MAX_HIS_VOLATILITY_PAGES)
        self.assertEqual(len(ctx.calls), pmcc_data_futu.MAX_HIS_VOLATILITY_PAGES)
        self.assertEqual(len(result["values"]), 3 * pmcc_data_futu.MAX_HIS_VOLATILITY_PAGES)

    def test_non_finite_and_missing_iv_values_are_dropped(self) -> None:
        frame = build_series_frame([30.0, 31.0, 32.0])
        frame["iv"] = [30.0, float("nan"), "not-a-number"]
        ctx = FakeQuoteContext([frame])

        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)

        self.assertEqual(result["values"], [30.0])
        self.assertEqual(result["start_date"], "2025-01-02")
        self.assertEqual(result["end_date"], "2025-01-02")

    def test_failure_modes_return_empty_values_without_raising(self) -> None:
        cases = {
            "none_context": None,
            "unsupported_context": NoVolatilityQuoteContext(),
            "error_ret": ErrorReturnQuoteContext(),
            "missing_iv_column": MissingIvColumnQuoteContext(),
            "raising_context": RaisingQuoteContext(fail_on_page=0),
        }
        for label, ctx in cases.items():
            with self.subTest(label=label):
                result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)
                self.assertEqual(result["values"], [], label)
                self.assertEqual(result["rows"], 0, label)
                self.assertIsNone(result["start_date"], label)
                self.assertIsNone(result["end_date"], label)
                self.assertIsNotNone(result["error"], label)

    def test_partial_pagination_failure_keeps_first_page(self) -> None:
        class FlakySecondPage(FakeQuoteContext):
            def get_option_underlying_his_volatility(
                self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
            ):
                validate_his_volatility_call(index_option_type, begin_time, end_time)
                self.calls.append({"page_req_key": page_req_key})
                if page_req_key is None:
                    return 0, build_series_frame([30.0] * 20), "next-page"
                return -1, "server error", None

        flaky = FlakySecondPage([])
        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, flaky)
        self.assertEqual(len(result["values"]), 20)
        self.assertEqual(result["pages"], 2)
        self.assertIsNone(result["error"])


class OpendDailyIvSeriesResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.history_path = self.tmp_dir / "pmcc_iv_history.json"
        self.memory_path = self.tmp_dir / "pmcc_iv_rank_memory.json"
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self._stack.enter_context(patch.object(pmcc, "IV_HISTORY_FILE", self.history_path))
        self._stack.enter_context(patch.object(pmcc, "IV_RANK_MEMORY_FILE", self.memory_path))

    def _analyze(self, ctx=None, current_iv=30.0):
        return pmcc.build_iv_rank_analysis(
            SYMBOL,
            current_iv,
            chain_iv_proxy=41.5,
            chain_iv_proxy_meta={"value": 41.5, "status": "OK"},
            config=pmcc.DEFAULT_CONFIG,
            ctx=ctx,
        )

    def test_good_series_returns_opend_priority_with_hand_computed_values(self) -> None:
        # 21 values 20.0 .. 40.0 plus a latest sample of 30.0.
        values = [float(value) for value in range(20, 41)] + [30.0]
        ctx = FakeQuoteContext([build_series_frame(values)])

        analysis = self._analyze(ctx=ctx)

        # rank: (30 - 20) / (40 - 20) * 100 = 50.0
        self.assertEqual(analysis["iv_rank"], 50.0)
        # percentile: 12 of 22 values are <= 30.0 -> 54.55
        self.assertEqual(analysis["iv_percentile"], 54.55)
        self.assertEqual(analysis["current_iv"], 30.0)
        self.assertEqual(analysis["priority"], "opend_daily_iv_series")
        self.assertEqual(analysis["source"], "futu_opend_his_volatility")
        self.assertEqual(
            analysis["method"],
            "current IV position within Futu OpenD daily IV series (min/max)",
        )
        self.assertEqual(analysis["lookback_count"], len(values))
        self.assertEqual(analysis["history_status"], "OK")
        self.assertEqual(analysis["history_start_date"], "2025-01-02")
        self.assertEqual(
            analysis["history_end_date"],
            (date(2025, 1, 2) + timedelta(days=len(values) - 1)).isoformat(),
        )
        self.assertEqual(analysis["chain_iv_rank_proxy"], 41.5)
        self.assertEqual(analysis["chain_iv_rank_proxy_meta"], {"value": 41.5, "status": "OK"})
        self.assertIs(analysis["is_true_historical_iv_rank"], True)
        self.assertIs(analysis["is_decision_usable_iv_rank"], True)
        self.assertEqual(len(ctx.calls), 1)

    def test_series_takes_priority_over_local_history(self) -> None:
        self.history_path.write_text(
            json.dumps(build_local_history_payload([50.0] * 30)), encoding="utf-8"
        )
        series_values = [30.0 + 0.1 * offset for offset in range(22)]
        ctx = FakeQuoteContext([build_series_frame(series_values)])

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "opend_daily_iv_series")
        self.assertEqual(analysis["current_iv"], series_values[-1])
        self.assertEqual(
            analysis["iv_rank"],
            pmcc.calculate_historical_iv_rank(series_values[-1], series_values),
        )
        self.assertEqual(analysis["lookback_count"], 22)

    def test_short_series_falls_through_to_unavailable(self) -> None:
        ctx = FakeQuoteContext([build_series_frame([30.0] * 19)])

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(analysis["priority"], "unavailable")
        self.assertIsNone(analysis["iv_rank"])
        self.assertIsNone(analysis["iv_percentile"])
        self.assertIs(analysis["is_decision_usable_iv_rank"], False)

    def test_short_series_falls_through_to_local_history(self) -> None:
        local_values = [20.0 + 0.5 * offset for offset in range(25)]
        self.history_path.write_text(
            json.dumps(build_local_history_payload(local_values)), encoding="utf-8"
        )
        ctx = FakeQuoteContext([build_series_frame([30.0] * 19)])

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "pmcc_auto_history")
        self.assertEqual(analysis["lookback_count"], 25)
        self.assertIn("historical IV min/max", analysis["method"])

    def test_empty_series_falls_through_to_fresh_memory(self) -> None:
        self.memory_path.write_text(
            json.dumps(
                {
                    "records": {
                        SYMBOL: {
                            "iv_rank": 66.0,
                            "iv_percentile": 61.0,
                            "updated_at": pmcc.datetime.now().isoformat(timespec="seconds"),
                            "source": "manual_override",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        ctx = FakeQuoteContext([pd.DataFrame()])

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "fresh_manual_memory_fallback")
        self.assertEqual(analysis["iv_rank"], 66.0)

    def test_raising_quote_context_falls_through_without_propagating(self) -> None:
        ctx = RaisingQuoteContext(fail_on_page=0)

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(ctx.calls, 1)
        self.assertEqual(analysis["priority"], "unavailable")
        self.assertIsNone(analysis["iv_rank"])

    def test_second_page_exception_discards_partial_series(self) -> None:
        # An exception on the second page aborts the fetch: the helper returns
        # empty values and the resolver falls through instead of crashing.
        ctx = RaisingQuoteContext(fail_on_page=1)
        series = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)

        self.assertEqual(ctx.calls, 2)
        self.assertEqual(series["values"], [])  # exception path returns empty values
        self.assertIsNotNone(series["error"])

        analysis = self._analyze(ctx=ctx)
        self.assertEqual(analysis["priority"], "unavailable")

    def test_ctx_none_keeps_todays_behaviour(self) -> None:
        local_values = [20.0 + 0.5 * offset for offset in range(25)]
        self.history_path.write_text(
            json.dumps(build_local_history_payload(local_values)), encoding="utf-8"
        )

        with patch.object(pmcc, "fetch_underlying_daily_iv_series") as series_mock:
            analysis = pmcc.build_iv_rank_analysis(
                SYMBOL,
                30.0,
                chain_iv_proxy=41.5,
                chain_iv_proxy_meta={"value": 41.5},
                config=pmcc.DEFAULT_CONFIG,
            )

        self.assertEqual(series_mock.call_count, 0)  # never queried without a ctx
        self.assertEqual(analysis["priority"], "pmcc_auto_history")
        self.assertEqual(analysis["lookback_count"], 25)
        self.assertEqual(analysis["current_iv"], 30.0)

    def test_ctx_none_without_history_stays_unavailable(self) -> None:
        analysis = self._analyze(ctx=None)

        self.assertEqual(analysis["priority"], "unavailable")
        self.assertIsNone(analysis["iv_rank"])

    def test_cli_override_still_wins_over_series(self) -> None:
        from dataclasses import replace

        config = replace(pmcc.DEFAULT_CONFIG, iv_rank_override=72.0)
        ctx = FakeQuoteContext([build_series_frame([30.0] * 22)])

        with patch.object(pmcc, "fetch_underlying_daily_iv_series") as series_mock:
            analysis = pmcc.build_iv_rank_analysis(
                SYMBOL,
                30.0,
                chain_iv_proxy=41.5,
                chain_iv_proxy_meta={"value": 41.5},
                config=config,
                ctx=ctx,
            )

        self.assertEqual(analysis["priority"], "cli_override")
        self.assertEqual(analysis["iv_rank"], 72.0)
        self.assertEqual(series_mock.call_count, 0)  # no API call needed when overridden
        self.assertEqual(ctx.calls, [])


class OpendDailyIvSeriesUnitTests(unittest.TestCase):
    """The API ``iv`` column is already in percent - nothing may rescale it."""

    VALUES = [25.0 + 0.5 * offset for offset in range(20)] + [30.0]

    def test_helper_returns_percent_scale_values_untouched(self) -> None:
        ctx = FakeQuoteContext([build_series_frame(self.VALUES)])

        result = pmcc_data_futu.fetch_underlying_daily_iv_series(SYMBOL, ctx)

        self.assertEqual(result["values"], self.VALUES)
        self.assertTrue(all(value >= 20.0 for value in result["values"]))

    def test_resolver_rank_matches_percent_scale_hand_computation(self) -> None:
        ctx = FakeQuoteContext([build_series_frame(self.VALUES)])

        with tempfile.TemporaryDirectory() as tmp:
            empty_history = Path(tmp) / "pmcc_iv_history.json"
            empty_memory = Path(tmp) / "pmcc_iv_rank_memory.json"
            with patch.object(pmcc, "IV_HISTORY_FILE", empty_history), patch.object(
                pmcc, "IV_RANK_MEMORY_FILE", empty_memory
            ):
                analysis = pmcc.build_iv_rank_analysis(
                    SYMBOL,
                    30.0,
                    chain_iv_proxy=None,
                    chain_iv_proxy_meta=None,
                    config=pmcc.DEFAULT_CONFIG,
                    ctx=ctx,
                )

        # min=25.0, max=34.5 on the 20-value ramp; latest sample is 30.0.
        expected_rank = round((30.0 - 25.0) / (34.5 - 25.0) * 100, 2)
        expected_percentile = round(
            sum(1 for value in self.VALUES if value <= 30.0) / len(self.VALUES) * 100, 2
        )
        self.assertEqual(expected_rank, 52.63)
        self.assertEqual(expected_percentile, 57.14)
        self.assertEqual(analysis["iv_rank"], expected_rank)
        self.assertEqual(analysis["iv_percentile"], expected_percentile)
        self.assertEqual(analysis["priority"], "opend_daily_iv_series")
        # The series' own latest IV is the "current" IV; a 100x rescale would show up here.
        self.assertEqual(analysis["current_iv"], 30.0)
        self.assertAlmostEqual(analysis["current_iv"], self.VALUES[-1], places=6)
        self.assertFalse(math.isclose(analysis["current_iv"], 0.30))

    def test_existing_formulas_are_reused_unchanged(self) -> None:
        self.assertEqual(
            pmcc.calculate_historical_iv_rank(30.0, self.VALUES),
            round((30.0 - 25.0) / (34.5 - 25.0) * 100, 2),
        )
        self.assertEqual(
            pmcc.calculate_historical_iv_percentile(30.0, self.VALUES), 57.14
        )
        self.assertIsNone(pmcc.calculate_historical_iv_rank(30.0, self.VALUES[:19]))


if __name__ == "__main__":
    unittest.main()
