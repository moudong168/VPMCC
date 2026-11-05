"""Tests for the Futu OpenD IV overview resolver priority.

Covers ``pmcc.data_futu.fetch_underlying_iv_overview`` and the new
``opend_overview`` branch of ``build_iv_rank_analysis`` in
futu_option_decision.py:

(a) a documented-shape overview DataFrame is parsed untouched (values already in
    percent) and the API is called with a LIST of symbols,
(b) a good overview wins over the daily his-volatility series and over local
    history,
(c) a failing overview falls through to the fixed daily-series branch,
(d) when both OpenD paths fail, the outcome carries ``opend_error`` and
    ``summarize_iv_rank_analysis`` prints it,
(e) every helper failure mode returns ``iv_rank`` None plus an error string and
    never raises,
(f) no test rescales percent values: 31.0 in stays 31.0 out.
"""

import contextlib
import json
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

# Documented fixture values (percent units, exactly as the Futu app shows them).
OVERVIEW_IV = 67.72
OVERVIEW_IV_RANK = 31.0
OVERVIEW_IV_PERCENTILE = 33.0

# Official signatures of the endpoints under test:
#   get_option_underlying_overview(code_list, index_option_type=IndexOptionType.NORMAL)
#   get_option_underlying_his_volatility(code, index_option_type=IndexOptionType.NORMAL,
#                                       begin_time=None, end_time=None, page_req_key=None)
DATE_LIKE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_OFFICIAL_SPAN_DAYS = 364


def install_futu_stub() -> None:
    futu_stub = types.ModuleType("futu")
    futu_stub.OpenQuoteContext = type("OpenQuoteContext", (), {})
    futu_stub.OptionType = type("OptionType", (), {"CALL": "CALL", "PUT": "PUT"})
    futu_stub.RET_OK = 0
    futu_stub.TrdEnv = type("TrdEnv", (), {"REAL": "REAL"})
    sys.modules["futu"] = futu_stub


# The helpers call ``from futu import RET_OK`` lazily, so a stub module must exist
# even when futu_option_decision imported fine through its own ImportError fallback.
if "futu" not in sys.modules:
    try:
        import futu  # noqa: F401  (installed futu-api)
    except ModuleNotFoundError:
        install_futu_stub()

import futu_option_decision as pmcc
from pmcc import data_futu as pmcc_data_futu
from pmcc.reports import summarize_iv_rank_analysis


def validate_his_volatility_call(index_option_type, begin_time, end_time) -> None:
    """Pin the real OpenD signature so a positional date argument fails loudly."""
    if isinstance(index_option_type, str) and DATE_LIKE_PATTERN.match(index_option_type):
        raise TypeError(
            "get_option_underlying_his_volatility() got a date-like string as "
            f"index_option_type={index_option_type!r}; begin_time/end_time must be keyword arguments"
        )
    if begin_time and end_time:
        span_days = (date.fromisoformat(str(end_time)) - date.fromisoformat(str(begin_time))).days
        if span_days > MAX_OFFICIAL_SPAN_DAYS:
            raise AssertionError(
                f"requested span {span_days}d exceeds the official maximum of {MAX_OFFICIAL_SPAN_DAYS}d"
            )


def build_overview_frame(
    symbol: str = SYMBOL,
    iv=OVERVIEW_IV,
    iv_rank=OVERVIEW_IV_RANK,
    iv_percentile=OVERVIEW_IV_PERCENTILE,
) -> pd.DataFrame:
    """DataFrame with the documented get_option_underlying_overview shape."""
    return pd.DataFrame(
        [
            {
                "code": symbol,
                "name": "MSFT",
                "iv": iv,
                "iv_rank": iv_rank,
                "iv_percentile": iv_percentile,
                "pre_iv": 63.5,
                "hv_30d": 28.4,
                "hv_30d_percentile": 40.0,
                "hv_60d": 27.1,
            }
        ]
    )


def build_series_frame(values, start_day: date = date(2025, 1, 2)) -> pd.DataFrame:
    """DataFrame with the documented get_option_underlying_his_volatility shape."""
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


def build_local_history_payload(values) -> dict:
    start = date(2024, 6, 3)
    history = [
        {"date": (start + timedelta(days=offset)).isoformat(), "iv": value}
        for offset, value in enumerate(values)
    ]
    return {"schema_version": 1, "records": {SYMBOL: {"history": history}}}


# 21-value ramp 20.0 .. 40.0 plus a latest sample of 30.0 -> rank 50.0, pct 54.55.
SERIES_VALUES = [float(value) for value in range(20, 41)] + [30.0]


class FakeOverviewQuoteContext:
    """Overview-capable context that can also serve the daily IV series."""

    def __init__(
        self,
        frame=None,
        ret: int = 0,
        detail: str = "",
        series_frame=None,
        series_ret: int = 0,
        series_detail: str = "",
    ) -> None:
        self.frame = build_overview_frame() if frame is None else frame
        self.ret = ret
        self.detail = detail
        self.series_frame = build_series_frame(SERIES_VALUES) if series_frame is None else series_frame
        self.series_ret = series_ret
        self.series_detail = series_detail
        self.overview_calls = []
        self.series_calls = []

    def get_option_underlying_overview(self, code_list, index_option_type="NORMAL"):
        self.overview_calls.append({"code_list": code_list, "index_option_type": index_option_type})
        if self.ret != 0:
            return self.ret, self.detail
        return self.ret, self.frame.copy()

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        self.series_calls.append(
            {
                "code": code,
                "begin_time": begin_time,
                "end_time": end_time,
                "page_req_key": page_req_key,
            }
        )
        if self.series_ret != 0:
            return self.series_ret, self.series_detail, None
        return 0, self.series_frame.copy(), None


class NoOverviewQuoteContext:
    """Quote context from an older futu-api: no overview endpoint at all."""

    def __init__(self) -> None:
        self.calls = 0

    def get_option_underlying_his_volatility(
        self, code, index_option_type="NORMAL", begin_time=None, end_time=None, page_req_key=None
    ):
        validate_his_volatility_call(index_option_type, begin_time, end_time)
        self.calls += 1
        return -1, "not subscribed", None


class RaisingOverviewQuoteContext:
    def get_option_underlying_overview(self, code_list, index_option_type="NORMAL"):
        raise RuntimeError("OpenD connection dropped")


class OpendIvOverviewHelperTests(unittest.TestCase):
    def test_helper_parses_documented_overview_shape_without_rescaling(self) -> None:
        ctx = FakeOverviewQuoteContext()

        result = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)

        self.assertEqual(result["iv"], OVERVIEW_IV)
        self.assertEqual(result["iv_rank"], OVERVIEW_IV_RANK)
        self.assertEqual(result["iv_percentile"], OVERVIEW_IV_PERCENTILE)
        self.assertIsNone(result["error"])
        # Percent units are returned untouched - a 100x rescale would show up here.
        self.assertEqual(result["iv_rank"], 31.0)
        self.assertNotEqual(result["iv_rank"], 0.31)
        self.assertEqual(result["iv"], 67.72)
        self.assertNotEqual(result["iv"], 0.6772)

    def test_helper_requests_the_symbol_as_a_list(self) -> None:
        ctx = FakeOverviewQuoteContext()

        pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)

        self.assertEqual(len(ctx.overview_calls), 1)
        self.assertEqual(ctx.overview_calls[0]["code_list"], [SYMBOL])
        self.assertEqual(ctx.overview_calls[0]["index_option_type"], "NORMAL")

    def test_helper_picks_the_matching_row_by_code(self) -> None:
        frame = pd.concat(
            [
                build_overview_frame(symbol="US.AAPL", iv=30.0, iv_rank=5.0, iv_percentile=6.0),
                build_overview_frame(),
            ],
            ignore_index=True,
        )
        ctx = FakeOverviewQuoteContext(frame=frame)

        result = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)

        self.assertEqual(result["iv_rank"], OVERVIEW_IV_RANK)
        self.assertEqual(result["iv_percentile"], OVERVIEW_IV_PERCENTILE)

    def test_helper_failure_modes_never_raise(self) -> None:
        cases = {
            "none_context": None,
            "unsupported_context": object(),
            "raising_context": RaisingOverviewQuoteContext(),
            "empty_frame": FakeOverviewQuoteContext(frame=pd.DataFrame()),
            "error_ret": FakeOverviewQuoteContext(ret=-1, detail="no permission"),
        }
        for label, ctx in cases.items():
            with self.subTest(label=label):
                result = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)
                self.assertIsNone(result["iv_rank"], label)
                self.assertIsNone(result["iv_percentile"], label)
                self.assertIsInstance(result["error"], str, label)
                self.assertTrue(result["error"], label)

    def test_helper_missing_iv_rank_column_keeps_parsed_columns(self) -> None:
        """A frame without iv_rank still yields the columns it does have."""
        frame = pd.DataFrame(
            [
                {
                    "code": SYMBOL,
                    "name": "MSFT",
                    "iv": OVERVIEW_IV,
                    "iv_percentile": OVERVIEW_IV_PERCENTILE,
                }
            ]
        )
        ctx = FakeOverviewQuoteContext(frame=frame)

        result = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)

        # Partial parse: iv_rank is missing, so it is None plus a descriptive error.
        self.assertIsNone(result["iv_rank"])
        self.assertIsInstance(result["error"], str)
        self.assertTrue(result["error"])
        self.assertIn("missing_iv_rank_column", result["error"])
        # ...but the present columns are NOT discarded.
        self.assertEqual(result["iv_percentile"], OVERVIEW_IV_PERCENTILE)
        self.assertEqual(result["iv"], OVERVIEW_IV)

    def test_helper_error_strings_are_descriptive(self) -> None:
        cases = {
            "missing_quote_context": None,
            "unsupported_quote_context": object(),
        }
        for expected, ctx in cases.items():
            with self.subTest(expected=expected):
                result = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, ctx)
                self.assertEqual(result["error"], expected)

        raising = pmcc_data_futu.fetch_underlying_iv_overview(SYMBOL, RaisingOverviewQuoteContext())
        self.assertIn("RuntimeError", raising["error"])
        self.assertIn("OpenD connection dropped", raising["error"])

        error_ret = pmcc_data_futu.fetch_underlying_iv_overview(
            SYMBOL, FakeOverviewQuoteContext(ret=-1, detail="no permission")
        )
        self.assertIn("ret=-1", error_ret["error"])
        self.assertIn("no permission", error_ret["error"])

        missing_column = pmcc_data_futu.fetch_underlying_iv_overview(
            SYMBOL, FakeOverviewQuoteContext(frame=pd.DataFrame([{"code": SYMBOL, "iv": OVERVIEW_IV}]))
        )
        self.assertEqual(missing_column["error"], "missing_iv_rank_column")


class OpendIvOverviewResolverTests(unittest.TestCase):
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

    def test_overview_returns_opend_overview_priority_untouched(self) -> None:
        ctx = FakeOverviewQuoteContext()

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["iv_rank"], OVERVIEW_IV_RANK)
        self.assertEqual(analysis["iv_percentile"], OVERVIEW_IV_PERCENTILE)
        self.assertEqual(analysis["iv_rank"], 31.0)
        self.assertEqual(analysis["current_iv"], OVERVIEW_IV)
        self.assertEqual(analysis["source"], "futu_opend_overview")
        self.assertEqual(analysis["priority"], "opend_overview")
        self.assertEqual(
            analysis["method"],
            "Futu OpenD option underlying overview (direct iv_rank / iv_percentile)",
        )
        self.assertEqual(analysis["chain_iv_rank_proxy"], 41.5)
        self.assertEqual(analysis["chain_iv_rank_proxy_meta"], {"value": 41.5, "status": "OK"})
        self.assertIs(analysis["is_true_historical_iv_rank"], True)
        self.assertIs(analysis["is_decision_usable_iv_rank"], True)
        self.assertNotIn("opend_error", analysis)
        self.assertEqual(ctx.overview_calls[0]["code_list"], [SYMBOL])

    def test_overview_wins_over_series_and_local_history(self) -> None:
        self.history_path.write_text(
            json.dumps(build_local_history_payload([50.0] * 30)), encoding="utf-8"
        )
        # The same context could answer the daily series as well.
        ctx = FakeOverviewQuoteContext()

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "opend_overview")
        self.assertEqual(analysis["source"], "futu_opend_overview")
        self.assertEqual(analysis["iv_rank"], 31.0)
        self.assertEqual(analysis["iv_percentile"], 33.0)
        self.assertEqual(analysis["current_iv"], OVERVIEW_IV)
        self.assertEqual(ctx.series_calls, [])  # overview short-circuits the fallback
        self.assertEqual(len(ctx.overview_calls), 1)

    def test_overview_missing_iv_uses_current_iv_argument(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "code": SYMBOL,
                    "name": "MSFT",
                    "iv_rank": OVERVIEW_IV_RANK,
                    "iv_percentile": OVERVIEW_IV_PERCENTILE,
                }
            ]
        )
        ctx = FakeOverviewQuoteContext(frame=frame)

        analysis = self._analyze(ctx=ctx, current_iv=30.0)

        self.assertEqual(analysis["priority"], "opend_overview")
        self.assertEqual(analysis["iv_rank"], 31.0)
        self.assertEqual(analysis["current_iv"], 30.0)

    def test_overview_error_falls_through_to_daily_series(self) -> None:
        ctx = FakeOverviewQuoteContext(ret=-1, detail="no permission for overview")

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "opend_daily_iv_series")
        self.assertEqual(analysis["source"], "futu_opend_his_volatility")
        self.assertEqual(len(ctx.overview_calls), 1)
        self.assertEqual(len(ctx.series_calls), 1)
        expected_rank = pmcc.calculate_historical_iv_rank(SERIES_VALUES[-1], SERIES_VALUES)
        self.assertEqual(analysis["iv_rank"], expected_rank)
        self.assertEqual(analysis["iv_rank"], 50.0)
        self.assertEqual(analysis["current_iv"], SERIES_VALUES[-1])
        self.assertNotIn("opend_error", analysis)

    def test_overview_and_series_failure_surface_opend_error(self) -> None:
        ctx = FakeOverviewQuoteContext(
            ret=-1,
            detail="overview denied",
            series_ret=-1,
            series_detail="his_volatility denied",
        )

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "unavailable")
        self.assertIsNone(analysis["iv_rank"])
        opend_error = analysis["opend_error"]
        self.assertIn("overview:", opend_error)
        self.assertIn("overview denied", opend_error)
        self.assertIn("his_volatility:", opend_error)
        self.assertIn("his_volatility denied", opend_error)

        summary = summarize_iv_rank_analysis(analysis)
        self.assertIn("OpenD error", summary)
        self.assertIn("overview denied", summary)
        self.assertIn("his_volatility denied", summary)

    def test_memory_fallback_summary_includes_opend_error(self) -> None:
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
        ctx = FakeOverviewQuoteContext(
            ret=-1, detail="overview denied", series_ret=-1, series_detail="his_volatility denied"
        )

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "fresh_manual_memory_fallback")
        self.assertEqual(analysis["iv_rank"], 66.0)
        self.assertIn("overview denied", analysis["opend_error"])
        summary = summarize_iv_rank_analysis(analysis)
        self.assertIn("OpenD error", summary)
        self.assertIn("his_volatility denied", summary)

    def test_short_series_with_failing_overview_reports_both_attempts(self) -> None:
        ctx = FakeOverviewQuoteContext(
            ret=-1, detail="overview denied", series_frame=build_series_frame([30.0] * 5)
        )

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "unavailable")
        # Both OpenD attempts were made and both failed - both are named.
        self.assertIn("overview: ", analysis["opend_error"])
        self.assertIn("his_volatility: insufficient_series_values(5)", analysis["opend_error"])

    def test_no_ctx_keeps_todays_behaviour_without_opend_error(self) -> None:
        with patch.object(pmcc, "fetch_underlying_iv_overview") as overview_mock, patch.object(
            pmcc, "fetch_underlying_daily_iv_series"
        ) as series_mock:
            analysis = pmcc.build_iv_rank_analysis(
                SYMBOL,
                30.0,
                chain_iv_proxy=41.5,
                chain_iv_proxy_meta={"value": 41.5},
                config=pmcc.DEFAULT_CONFIG,
            )

        self.assertEqual(overview_mock.call_count, 0)
        self.assertEqual(series_mock.call_count, 0)
        self.assertEqual(analysis["priority"], "unavailable")
        self.assertNotIn("opend_error", analysis)

    def test_cli_override_still_wins_over_overview(self) -> None:
        from dataclasses import replace

        config = replace(pmcc.DEFAULT_CONFIG, iv_rank_override=72.0)
        ctx = FakeOverviewQuoteContext()

        with patch.object(pmcc, "fetch_underlying_iv_overview") as overview_mock:
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
        self.assertEqual(overview_mock.call_count, 0)
        self.assertEqual(ctx.overview_calls, [])

    def test_local_history_still_used_when_opend_paths_fail(self) -> None:
        local_values = [20.0 + 0.5 * offset for offset in range(25)]
        self.history_path.write_text(
            json.dumps(build_local_history_payload(local_values)), encoding="utf-8"
        )
        ctx = NoOverviewQuoteContext()

        analysis = self._analyze(ctx=ctx)

        self.assertEqual(analysis["priority"], "pmcc_auto_history")
        self.assertEqual(analysis["lookback_count"], 25)
        # A successful (non-degraded) branch carries no OpenD error noise.
        self.assertNotIn("opend_error", analysis)


class OpendIvOverviewReportTextTests(unittest.TestCase):
    """``summarize_iv_rank_analysis`` only adds the error when it is present."""

    def test_unavailable_summary_without_opend_error_is_unchanged(self) -> None:
        analysis = {
            "iv_rank": None,
            "iv_percentile": None,
            "history_status": "MISSING",
            "chain_iv_rank_proxy": None,
            "chain_iv_rank_proxy_meta": {},
            "is_decision_usable_iv_rank": False,
        }
        self.assertEqual(
            summarize_iv_rank_analysis(analysis),
            "historical IV Rank unavailable (MISSING)",
        )

    def test_unavailable_summary_appends_opend_error(self) -> None:
        analysis = {
            "iv_rank": None,
            "iv_percentile": None,
            "history_status": "MISSING",
            "chain_iv_rank_proxy": None,
            "chain_iv_rank_proxy_meta": {},
            "is_decision_usable_iv_rank": False,
            "opend_error": "overview: ret=-1, detail=denied; his_volatility: ret=-1, detail=denied",
        }
        summary = summarize_iv_rank_analysis(analysis)
        self.assertTrue(
            summary.endswith(
                "; OpenD error: overview: ret=-1, detail=denied; his_volatility: ret=-1, detail=denied"
            )
        )


if __name__ == "__main__":
    unittest.main()
