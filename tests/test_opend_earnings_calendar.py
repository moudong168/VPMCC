"""Tests for the Futu OpenD earnings calendar as the primary earnings source.

Covers the data-source tiering change:

(a) ``fetch_opend_earnings_calendar`` scans the 98-day horizon in consecutive
    windows whose ``end_date - begin_date`` never exceeds the official 7-day API
    limit, filters rows locally on the security column (``US.NVDA`` / ``NVDA``)
    and keeps the nearest date >= today,
(b) a RET_ERROR comes back as ``status='ERROR'`` with the error text (never a
    silent success, never a raise) and the web validation path still works,
(c) an OK OpenD date wins over differing web dates and raises ``source_conflict``,
(d) two agreeing web sources without OpenD are primary as ``web_agreed`` +
    confirmed,
(e) a single web source is unconfirmed and the event gate downgrades it to a
    warning instead of a hard block,
(f) every validation source record carries a tier label and the report banner
    differs between opend and cboe modes.

The fake quote context deliberately implements the official v10.12 signature
``get_earnings_calendar(market, sort_type=None, begin_date=None, end_date=None,
filter_list=None)`` and raises ``TypeError`` for positional calls or windows
larger than 7 days: a fake that accepts a wrong signature hides production bugs.
"""

import json
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

SYMBOL = "US.NVDA"
MAX_OFFICIAL_WINDOW_DAYS = 7

# The production helpers import ``Market``/``RET_OK`` lazily, so make sure the
# futu module exposes them even when futu-api is not installed in the test env.
try:  # pragma: no cover - depends on the environment
    import futu as futu_module
except ModuleNotFoundError:  # pragma: no cover
    futu_module = None
if futu_module is None:
    futu_module = types.ModuleType("futu")
    futu_module.RET_OK = 0
    futu_module.TrdEnv = type("TrdEnv", (), {"REAL": "REAL"})
    futu_module.OptionType = type("OptionType", (), {"CALL": "CALL", "PUT": "PUT"})
    sys.modules["futu"] = futu_module
if not hasattr(futu_module, "Market"):  # another test module may have installed a stub
    futu_module.Market = type("Market", (), {"US": "US"})

import futu_option_decision as pmcc
from pmcc import data_futu as pmcc_data_futu
from pmcc import reports as pmcc_reports
from pmcc import web_validation as pmcc_web_validation


class FakeEarningsQuoteContext:
    """Strict fake of the official ``get_earnings_calendar`` quote-only endpoint.

    The keyword names/defaults mirror the official v10.12 signature
    ``get_earnings_calendar(market, sort_type=None, begin_date=None, end_date=None,
    filter_list=None)``; any positional argument (which would shift the dates into
    the wrong slots) or a window wider than 7 days raises ``TypeError``.
    """

    def __init__(self, rows_by_offset=None, ret=None, error_data=None):
        self.rows_by_offset = rows_by_offset or {}
        self.ret = ret
        self.error_data = error_data if error_data is not None else "permission denied"
        self.calls = []

    def get_earnings_calendar(
        self,
        *args,
        market=None,
        sort_type=None,
        begin_date=None,
        end_date=None,
        filter_list=None,
    ):
        if args:
            raise TypeError(
                "get_earnings_calendar() accepts keyword arguments only "
                "(market=..., sort_type=..., begin_date=..., end_date=..., filter_list=...)"
            )
        if begin_date is None or end_date is None:
            raise TypeError("get_earnings_calendar() requires begin_date= and end_date= keyword arguments")
        begin = date.fromisoformat(str(begin_date))
        end = date.fromisoformat(str(end_date))
        span_days = (end - begin).days
        if span_days > MAX_OFFICIAL_WINDOW_DAYS:
            raise TypeError(
                f"get_earnings_calendar() window span {span_days}d exceeds the official "
                f"{MAX_OFFICIAL_WINDOW_DAYS}d limit"
            )
        if span_days < 0:
            raise TypeError("get_earnings_calendar() end_date precedes begin_date")
        self.calls.append(
            {
                "market": market,
                "begin_date": begin,
                "end_date": end,
                "span_days": span_days,
            }
        )
        if self.ret is not None:
            return self.ret, self.error_data
        offset = (begin - date.today()).days
        rows = self.rows_by_offset.get(offset, [])
        return 0, pd.DataFrame(rows, columns=["security", "name", "earnings_date", "pub_type"])


def earnings_row(security: str, offset_days: int, pub_type: str = "AFTER") -> dict:
    return {
        "security": security,
        "name": security.split(".")[-1],
        "earnings_date": (date.today() + timedelta(days=offset_days)).isoformat(),
        "pub_type": pub_type,
    }


def web_record(name: str, source: str, offset_days: int) -> dict:
    return {
        "name": name,
        "source": source,
        "status": "OK",
        "tier": "event",
        "next_earnings_date": (date.today() + timedelta(days=offset_days)).isoformat(),
        "days_to_earnings": offset_days,
        "time_of_day": "after_market",
        "confirmation_status": None,
    }


def build_validation(quote_ctx=None, next_earnings=None, wallstreet=None, manual_events=None):
    yahoo = {"name": "Yahoo Finance", "source": "yahoo_finance", "status": "OK", "price": 100.0, "tier": "check"}
    with patch.object(pmcc_web_validation, "fetch_yahoo_finance_quote", return_value=yahoo), patch.object(
        pmcc_web_validation, "fetch_next_earnings_date", return_value=next_earnings
    ), patch.object(
        pmcc_web_validation, "fetch_wallstreethorizon_earnings_date", return_value=wallstreet
    ):
        return pmcc_web_validation.build_market_data_validation(
            SYMBOL, 100.0, quote_ctx=quote_ctx, manual_events=manual_events
        )


def error_web_record(name: str, source: str) -> dict:
    return {"name": name, "source": source, "status": "ERROR", "tier": "event", "error": "offline"}


def build_event_block(validation):
    """Gate helper: ignore the repository's real local event calendar file."""
    with patch.object(pmcc, "load_local_event_records", return_value=[]):
        return pmcc.build_event_risk_block(SYMBOL, pd.Series({}), validation, pmcc.DEFAULT_CONFIG)


class FetchOpendEarningsCalendarTests(unittest.TestCase):
    def test_scans_weekly_windows_and_picks_nearest_future_date(self) -> None:
        ctx = FakeEarningsQuoteContext(
            rows_by_offset={
                0: [earnings_row("US.AAPL", 3, "BEFORE")],
                7: [],
                14: [
                    earnings_row("US.NVDA", -2),
                    earnings_row("US.NVDA", 17, "AFTER"),
                    earnings_row("US.NVDA", 18, "BEFORE"),
                ],
            }
        )

        record = pmcc_data_futu.fetch_opend_earnings_calendar(SYMBOL, ctx)

        self.assertEqual(record["status"], "OK")
        self.assertEqual(record["source"], "futu_opend_earnings")
        self.assertEqual(record["name"], "Futu OpenD Earnings Calendar")
        self.assertEqual(record["tier"], "primary")
        self.assertEqual(record["next_earnings_date"], (date.today() + timedelta(days=17)).isoformat())
        self.assertEqual(record["days_to_earnings"], 17)
        self.assertEqual(record["time_of_day"], "after_market")
        self.assertEqual(record["confirmation_status"], "opend")
        self.assertNotIn("error", record)

        # Third weekly window, every window inside the hard 7-day API limit.
        self.assertEqual(len(ctx.calls), 3)
        self.assertLessEqual(len(ctx.calls), 98 // 7 + 1)
        for call in ctx.calls:
            self.assertLessEqual(call["span_days"], MAX_OFFICIAL_WINDOW_DAYS)
            self.assertLessEqual(call["end_date"], date.today() + timedelta(days=98))
        self.assertEqual(ctx.calls[0]["begin_date"], date.today())
        self.assertEqual(ctx.calls[0]["end_date"], date.today() + timedelta(days=6))
        self.assertEqual(ctx.calls[1]["begin_date"], date.today() + timedelta(days=7))

    def test_accepts_bare_symbol_and_maps_pub_type(self) -> None:
        ctx = FakeEarningsQuoteContext(rows_by_offset={0: [earnings_row("NVDA", 4, "REGULAR")]})

        record = pmcc_data_futu.fetch_opend_earnings_calendar("NVDA", ctx)

        self.assertEqual(record["status"], "OK")
        self.assertEqual(record["time_of_day"], "intraday")
        self.assertEqual(record["days_to_earnings"], 4)

    def test_no_rows_within_horizon_returns_error_without_raising(self) -> None:
        ctx = FakeEarningsQuoteContext(rows_by_offset={})

        record = pmcc_data_futu.fetch_opend_earnings_calendar(SYMBOL, ctx, horizon_days=14)

        self.assertEqual(record["status"], "ERROR")
        self.assertIn("no earnings date", record["error"])
        self.assertEqual(len(ctx.calls), 2)
        for call in ctx.calls:
            self.assertLessEqual(call["span_days"], MAX_OFFICIAL_WINDOW_DAYS)

    def test_missing_context_returns_error_instead_of_raising(self) -> None:
        record = pmcc_data_futu.fetch_opend_earnings_calendar(SYMBOL, None)

        self.assertEqual(record["status"], "ERROR")
        self.assertEqual(record["error"], "missing_quote_context")


class FakeSignatureStrictnessTests(unittest.TestCase):
    def test_fake_rejects_positional_calls_and_oversized_windows(self) -> None:
        ctx = FakeEarningsQuoteContext()
        with self.assertRaises(TypeError):
            ctx.get_earnings_calendar("US", "2026-01-01", "2026-01-07")  # positional
        with self.assertRaises(TypeError):
            ctx.get_earnings_calendar(
                market="US",
                begin_date=date.today().isoformat(),
                end_date=(date.today() + timedelta(days=9)).isoformat(),
            )
        self.assertEqual(ctx.calls, [])


class EarningsPrecedenceTests(unittest.TestCase):
    def test_opend_error_is_reported_and_web_path_still_works(self) -> None:
        ctx = FakeEarningsQuoteContext(ret=1, error_data="RET_ERROR: no quote permission")

        record = pmcc_data_futu.fetch_opend_earnings_calendar(SYMBOL, ctx)

        self.assertEqual(record["status"], "ERROR")
        self.assertEqual(record["tier"], "primary")
        self.assertIn("ret=1", record["error"])
        self.assertNotEqual(record.get("next_earnings_date"), "set-by-accident")

        validation = build_validation(
            quote_ctx=ctx,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", 20),
            wallstreet=web_record("Wall Street Horizon", "wall_street_horizon_earnings", 20),
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "web_agreed")
        self.assertTrue(earnings["confirmed"])
        opend_sources = [item for item in validation["sources"] if item.get("source") == "futu_opend_earnings"]
        self.assertEqual(len(opend_sources), 1)
        self.assertEqual(opend_sources[0]["status"], "ERROR")
        self.assertIn("ret=1", opend_sources[0]["error"])

    def test_opend_date_is_primary_and_conflict_is_flagged(self) -> None:
        ctx = FakeEarningsQuoteContext(rows_by_offset={0: [earnings_row(SYMBOL, 10, "AFTER")]})
        web_days = 25

        validation = build_validation(
            quote_ctx=ctx,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", web_days),
            wallstreet=web_record("Wall Street Horizon", "wall_street_horizon_earnings", web_days),
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "futu_opend_earnings")
        self.assertEqual(earnings["days_to_earnings"], 10)
        self.assertEqual(earnings["confirmation_status"], "opend")
        self.assertTrue(earnings["confirmed"])
        self.assertTrue(earnings["source_conflict"])

        conflict_dates = {item["date"] for item in earnings["candidate_dates"]}
        self.assertIn((date.today() + timedelta(days=10)).isoformat(), conflict_dates)
        self.assertIn((date.today() + timedelta(days=web_days)).isoformat(), conflict_dates)
        self.assertTrue(earnings["conflict_dates"])
        self.assertIn(
            "futu_opend_earnings",
            {item["source"] for item in earnings["conflict_dates"]},
        )

        # Confirmed + inside earnings_block_days -> still a hard block.
        event_block = build_event_block(validation)
        self.assertTrue(event_block["blocked"])
        self.assertEqual(event_block["blocking_events"][0]["type"], "earnings")

    def test_agreeing_web_sources_are_primary_without_opend(self) -> None:
        validation = build_validation(
            quote_ctx=None,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", 30),
            wallstreet=web_record("Wall Street Horizon", "wall_street_horizon_earnings", 30),
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "web_agreed")
        self.assertEqual(earnings["source_name"], "Web sources agreed")
        self.assertTrue(earnings["confirmed"])
        self.assertFalse(earnings["source_conflict"])
        self.assertEqual(earnings["days_to_earnings"], 30)
        self.assertNotIn("futu_opend_earnings", [item.get("source") for item in validation["sources"]])

    def test_disagreeing_web_sources_stay_unconfirmed_and_flag_conflict(self) -> None:
        validation = build_validation(
            quote_ctx=None,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", 30),
            wallstreet=web_record("Wall Street Horizon", "wall_street_horizon_earnings", 40),
        )

        earnings = validation["earnings_check"]
        self.assertFalse(earnings["confirmed"])
        self.assertTrue(earnings["source_conflict"])
        self.assertEqual(earnings["days_to_earnings"], 30)

    def test_single_web_source_is_unconfirmed_and_gate_only_warns(self) -> None:
        validation = build_validation(
            quote_ctx=None,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", 5),
            wallstreet=error_web_record("Wall Street Horizon", "wall_street_horizon_earnings"),
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "next_earnings_date")
        self.assertFalse(earnings["confirmed"])

        event_block = build_event_block(validation)
        self.assertFalse(event_block["blocked"])
        self.assertEqual(event_block["blocking_events"], [])
        warned = [event for event in event_block["attention_events"] if event.get("type") == "earnings"]
        self.assertEqual(len(warned), 1)
        self.assertTrue(warned[0]["warning_only"])
        self.assertIn("unconfirmed", pmcc_reports.summarize_event_block(event_block).lower())

        confirmed_validation = {"earnings_check": dict(earnings, confirmed=True, source="futu_opend_earnings")}
        confirmed_block = build_event_block(confirmed_validation)
        self.assertTrue(confirmed_block["blocked"])

    def test_manual_confirmed_entry_counts_as_confirmed(self) -> None:
        manual_events = [
            {"type": "earnings", "date": (date.today() + timedelta(days=6)).isoformat(), "days_to_event": 6, "source": "manual_confirmed"},
            {"type": "earnings", "date": (date.today() + timedelta(days=30)).isoformat(), "days_to_event": 30, "source": "local_event_calendar"},
        ]

        validation = build_validation(
            quote_ctx=None,
            next_earnings=error_web_record("NextEarningsDate", "next_earnings_date"),
            wallstreet=error_web_record("Wall Street Horizon", "wall_street_horizon_earnings"),
            manual_events=manual_events,
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "manual_confirmed")
        self.assertTrue(earnings["confirmed"])
        self.assertTrue(earnings["source_conflict"] is False)
        event_block = build_event_block(validation)
        self.assertTrue(event_block["blocked"])

    def test_plain_manual_entry_is_unconfirmed(self) -> None:
        manual_events = [
            {"type": "earnings", "date": (date.today() + timedelta(days=6)).isoformat(), "days_to_event": 6, "source": "local_event_calendar"},
        ]

        validation = build_validation(
            quote_ctx=None,
            next_earnings=error_web_record("NextEarningsDate", "next_earnings_date"),
            wallstreet=error_web_record("Wall Street Horizon", "wall_street_horizon_earnings"),
            manual_events=manual_events,
        )

        earnings = validation["earnings_check"]
        self.assertEqual(earnings["source"], "local_event_calendar")
        self.assertFalse(earnings["confirmed"])
        event_block = build_event_block(validation)
        self.assertFalse(event_block["blocked"])

    def test_local_calendar_loader_marks_manual_confirmed_entries(self) -> None:
        future_6 = (date.today() + timedelta(days=6)).isoformat()
        future_30 = (date.today() + timedelta(days=30)).isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar_path = Path(tmpdir) / "pmcc_event_calendar.json"
            calendar_path.write_text(
                json.dumps(
                    {
                        "US.TEST": [
                            {"type": "earnings", "date": future_6, "source": "manual_confirmed"},
                            {"type": "earnings", "date": future_30, "source": "local_event_calendar"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(pmcc, "EVENT_CALENDAR_FILE", calendar_path):
                records = pmcc.load_local_event_records("US.TEST")
                block = pmcc.build_event_risk_block(
                    "US.TEST", pd.Series({}), {"earnings_check": {"status": "UNAVAILABLE"}}, pmcc.DEFAULT_CONFIG
                )

        by_date = {record["date"]: record for record in records}
        self.assertTrue(by_date[future_6]["confirmed"])
        self.assertEqual(by_date[future_6]["tier"], "manual")
        self.assertFalse(by_date[future_30]["confirmed"])
        self.assertTrue(block["blocked"])
        self.assertEqual([event["date"] for event in block["blocking_events"]], [future_6])


class TierLabelAndBannerTests(unittest.TestCase):
    def test_validation_source_records_carry_tiers(self) -> None:
        ctx = FakeEarningsQuoteContext(rows_by_offset={0: [earnings_row(SYMBOL, 12)]})

        validation = build_validation(
            quote_ctx=ctx,
            next_earnings=web_record("NextEarningsDate", "next_earnings_date", 12),
            wallstreet=web_record("Wall Street Horizon", "wall_street_horizon_earnings", 12),
            manual_events=[
                {"type": "earnings", "date": (date.today() + timedelta(days=12)).isoformat(), "days_to_event": 12, "source": "local_event_calendar"}
            ],
        )

        tiers = {item.get("tier") for item in validation["sources"]}
        self.assertEqual(tiers, {"primary", "check", "event", "manual"})
        self.assertEqual(pmcc_reports.tier_label("primary"), "[主源]")
        self.assertEqual(pmcc_reports.tier_label("check"), "[校验]")
        self.assertEqual(pmcc_reports.tier_label("event"), "[事件]")
        self.assertEqual(pmcc_reports.tier_label("manual"), "[手输]")

    def test_html_report_lists_tier_labels_and_banner(self) -> None:
        earnings_check = {
            "status": "WARN",
            "next_earnings_date": (date.today() + timedelta(days=5)).isoformat(),
            "days_to_earnings": 5,
            "source": "futu_opend_earnings",
            "source_name": "Futu OpenD Earnings Calendar",
            "confirmed": True,
            "source_conflict": True,
            "candidate_dates": [
                {"source": "futu_opend_earnings", "name": "Futu OpenD Earnings Calendar", "tier": "primary", "date": "2026-06-05"},
                {"source": "next_earnings_date", "name": "NextEarningsDate", "tier": "event", "date": "2026-06-12"},
            ],
            "conflict_dates": [
                {"source": "futu_opend_earnings", "name": "Futu OpenD Earnings Calendar", "tier": "primary", "date": "2026-06-05"},
                {"source": "next_earnings_date", "name": "NextEarningsDate", "tier": "event", "date": "2026-06-12"},
            ],
        }
        position = {
            "symbol": SYMBOL,
            "price": 219.51,
            "coverage": {"base_contracts": 1, "short_call_contracts": 1, "available_to_sell": 0},
            "iv_environment": {"label": "FAVORABLE"},
            "data_validation": {
                "enabled": True,
                "price_check": {"status": "OK"},
                "earnings_check": earnings_check,
                "sources": [
                    {"name": "Futu OpenD Earnings Calendar", "source": "futu_opend_earnings", "status": "OK", "tier": "primary"},
                    {"name": "Yahoo Finance", "source": "yahoo_finance", "status": "OK", "tier": "check"},
                    {"name": "NextEarningsDate", "source": "next_earnings_date", "status": "ERROR", "error": "offline", "tier": "event"},
                    {"name": "Local Event Calendar", "source": "local_event_calendar", "status": "OK", "tier": "manual"},
                ],
            },
            "event_block": {"blocked": False, "attention_events": [{"type": "earnings", "days_to_event": 5}]},
        }
        recommendation = {
            "symbol": SYMBOL,
            "action": "WAIT",
            "reason": ["unit test"],
            "candidate_short_call": {"code": "US.NVDA260618C240000", "consider_selling": False},
            "short_call_reviews": [],
            "short_put_reviews": [],
            "summary_cn": "unit test summary",
        }
        result = {
            "mode": "pmcc_two_stage",
            "data_mode": "opend",
            "position_source": {"source": "unit_test", "total_base_contracts": 1},
            "recorded_positions": {
                "base_positions": [{"underlying": "US.NVDA", "code": "US.NVDA270617C155000", "quantity": 1}],
                "short_calls": [{"underlying": "US.NVDA", "code": "US.NVDA260618C240000", "quantity": 1}],
            },
            "stage_1_position_analysis": [position],
            "stage_2_operation_recommendations": [recommendation],
        }

        opened_html = pmcc_reports.build_html_report(result, generated_at="2026-06-01 09:00:00")
        self.assertIn("[主源]", opened_html)
        self.assertIn("[校验]", opened_html)
        self.assertIn("[事件]", opened_html)
        self.assertIn("[手输]", opened_html)
        self.assertIn("Futu OpenD 实时 (主源)", opened_html)
        self.assertNotIn("注意: 本报告基于 CBOE 延迟数据 (约15分钟)", opened_html)
        self.assertIn("财报日期来源冲突", opened_html)
        self.assertIn("2026-06-12", opened_html)

        cboe_result = dict(result, data_mode="cboe")
        cboe_html = pmcc_reports.build_html_report(cboe_result, generated_at="2026-06-01 09:00:00")
        self.assertIn("注意: 本报告基于 CBOE 延迟数据 (约15分钟)", cboe_html)
        self.assertNotEqual(
            pmcc_reports.format_data_source_banner(result),
            pmcc_reports.format_data_source_banner(cboe_result),
        )

        csv_result = {
            "mode": "pmcc_two_stage_multi_portfolio",
            "data_mode": "csv",
            "position_source": {
                "source": "csv_positions_no_opend",
                "csv_files": [{"broker": "Futu", "file": "持仓-20260601-090000.csv", "date": "2026-06-01"}],
            },
        }
        csv_banner = pmcc_reports.format_data_source_banner(csv_result)
        self.assertIn("CSV 持仓文件", csv_banner)
        self.assertIn("持仓-20260601-090000.csv", csv_banner)
        self.assertIn("2026-06-01", csv_banner)


if __name__ == "__main__":
    unittest.main()
