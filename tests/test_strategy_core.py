import importlib.util
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


def install_futu_stub() -> None:
    futu_stub = types.ModuleType("futu")

    class _FutuPlaceholder:
        pass

    futu_stub.KLType = type("KLType", (), {"K_DAY": "K_DAY"})
    futu_stub.OpenQuoteContext = _FutuPlaceholder
    futu_stub.OpenSecTradeContext = _FutuPlaceholder
    futu_stub.OptionType = type("OptionType", (), {"CALL": "CALL", "PUT": "PUT"})
    futu_stub.RET_OK = 0
    futu_stub.SubType = type("SubType", (), {"K_DAY": "K_DAY"})
    futu_stub.TrdEnv = type("TrdEnv", (), {"REAL": "REAL"})
    futu_stub.TrdMarket = type("TrdMarket", (), {"US": "US"})
    sys.modules["futu"] = futu_stub


try:
    import futu_option_decision as pmcc
except ModuleNotFoundError as exc:
    if exc.name != "futu":
        raise
    install_futu_stub()
    import futu_option_decision as pmcc

from pmcc import utils as pmcc_utils
from pmcc import memory as pmcc_memory
from pmcc import interaction as pmcc_interaction
from pmcc import reports as pmcc_reports
from pmcc import positions as pmcc_positions
from pmcc import constants as pmcc_constants
from pmcc import data_quality as pmcc_data_quality
from pmcc import strategy as pmcc_strategy
from pmcc import iv as pmcc_iv
from pmcc import web_validation as pmcc_web_validation
from pmcc import data_futu as pmcc_data_futu
from pmcc import trade_journal as pmcc_trade_journal
from pmcc import roll_pnl as pmcc_roll_pnl


class StrategyCoreTests(unittest.TestCase):
    def test_interaction_io_wrappers_preserve_prompt_behavior(self) -> None:
        with patch("builtins.input", return_value="  value  "):
            self.assertEqual(pmcc_interaction.read_interactive_text("Prompt: "), "value")

    def test_utils_module_preserves_safe_conversion_helpers(self) -> None:
        self.assertEqual(pmcc_utils.safe_float("1.25"), 1.25)
        self.assertIsNone(pmcc_utils.safe_float("bad"))
        self.assertEqual(pmcc_utils.safe_int(1.6), 2)
        self.assertEqual(pmcc_utils.safe_text("  US.NVDA  "), "US.NVDA")
        self.assertEqual(pmcc_utils.parse_expiry("2026-06-18").isoformat(), "2026-06-18")
        self.assertEqual(pmcc_utils.symbol_to_web_ticker("US.NVDA"), "NVDA")

    def test_constants_module_preserves_core_values(self) -> None:
        self.assertEqual(pmcc_constants.VALID_TRENDS, {"UP", "DOWN", "FLAT", "UNKNOWN"})
        self.assertEqual(pmcc_constants.IV_HISTORY_LOOKBACK_LIMIT, 252)

    def test_data_quality_module_preserves_blocking_shape(self) -> None:
        enriched = pd.DataFrame({"code": ["US.NVDA260618C240000"], "delta": [None], "implied_volatility": [None]})
        quality = pmcc_data_quality.build_option_data_quality("US.NVDA", enriched)
        self.assertEqual(quality["status"], "BLOCKED")
        self.assertIn("issues", quality)

    def test_strategy_module_preserves_basic_scoring(self) -> None:
        config = pmcc.StrategyConfig()
        self.assertEqual(pmcc_strategy.score_dte(None, config), -2.0)
        self.assertLess(pmcc_strategy.score_moneyness(-0.01), pmcc_strategy.score_moneyness(0.03))
        options = pd.DataFrame(
            [
                {
                    "delta": 0.3,
                    "days_to_expiry": 30,
                    "otm_pct": 0.04,
                    "bid_price": 1.0,
                    "ask_price": 1.1,
                    "last_price": 1.05,
                    "volume": 100,
                    "open_interest": 500,
                    "distance_from_spot": 4.0,
                    "strike_price": 104.0,
                    "is_otm": True,
                }
            ]
        )
        self.assertEqual(pmcc_strategy.select_option(options, config)["strike_price"], 104.0)
        structure = pmcc_strategy.evaluate_candidate_structure(pmcc_strategy.select_option(options, config), config)
        self.assertTrue(structure["structure_ok"])
        self.assertEqual(pmcc_strategy.build_action(80, "FLAT", 100.0, pmcc_strategy.select_option(options, config), config)["action"], "SELL_CALL")

    def test_iv_module_preserves_environment_shape(self) -> None:
        result = pmcc_iv.build_iv_environment(45.0, 35.0, 60.0, 60.0, {"implied_volatility": 48.0})
        self.assertEqual(result["label"], "FAVORABLE")
        self.assertEqual(result["iv_hv_ratio"], 1.286)

    def test_web_validation_helpers_preserve_text_and_source_selection(self) -> None:
        self.assertEqual(pmcc_web_validation.visible_page_text("<script>x</script><b>A&nbsp;B</b>"), "A B")
        self.assertEqual(pmcc_web_validation.wallstreethorizon_slug("NVDA"), "nvidia")
        selected = pmcc_web_validation.choose_primary_earnings_source(
            [
                {"status": "OK", "days_to_earnings": 10, "confirmation_status": None},
                {"status": "OK", "days_to_earnings": 5, "confirmation_status": "confirmed"},
            ]
        )
        self.assertEqual(selected["days_to_earnings"], 5)
        disabled = pmcc_web_validation.build_market_data_validation("US.NVDA", 100.0, enabled=False)
        self.assertEqual(disabled["price_check"]["status"], "SKIPPED")
        with patch.object(pmcc_web_validation, "fetch_yahoo_finance_quote", return_value={"status": "ERROR", "error": "offline"}), patch.object(
            pmcc_web_validation,
            "fetch_next_earnings_date",
            return_value={"name": "NextEarningsDate", "source": "next_earnings_date", "status": "ERROR", "error": "offline"},
        ), patch.object(
            pmcc_web_validation,
            "fetch_wallstreethorizon_earnings_date",
            return_value={"name": "Wall Street Horizon", "source": "wall_street_horizon_earnings", "status": "ERROR", "error": "offline"},
        ):
            validation = pmcc_web_validation.build_market_data_validation("US.NVDA", 100.0)
        self.assertNotIn("tipranks_earnings", [item.get("source") for item in validation["sources"]])

    def test_data_futu_helpers_preserve_error_and_chain_normalization(self) -> None:
        self.assertIn("ret=1", pmcc_data_futu.format_futu_error(1, pd.DataFrame()))
        self.assertIn(
            "questionnaire/agreement",
            pmcc_data_futu.enrich_error_message("请完成问卷：https://openapi.futunn.com"),
        )
        self.assertIn(
            "quote permission",
            pmcc_data_futu.enrich_error_message("无权限访问美股行情"),
        )
        plain_error = "Failed to get quote for US.NVDA: ret=1, detail=temporary failure"
        self.assertEqual(pmcc_data_futu.enrich_error_message(plain_error), plain_error)
        normalized = pmcc_data_futu.normalize_option_chain(pd.DataFrame({"strike_price": ["105", "100"], "code": ["B", "A"]}))
        self.assertEqual(normalized["code"].tolist(), ["A", "B"])
        class FakeQuoteContext:
            def get_option_expiration_date(self, symbol):
                return 0, pd.DataFrame({"strike_time": ["2026-06-18"], "option_expiry_date_distance": [24]})
            def get_market_snapshot(self, codes):
                return 0, pd.DataFrame({"code": codes, "option_delta": [0.25], "bid_price": [1.0], "ask_price": [1.1]})

        self.assertEqual(pmcc_data_futu.get_preferred_expiry_dates("US.NVDA", FakeQuoteContext(), pmcc.DEFAULT_CONFIG), ["2026-06-18"])
        greeks = pmcc_data_futu.get_greeks(["US.NVDA260618C240000"], FakeQuoteContext())
        self.assertEqual(greeks["delta"].iloc[0], 0.25)

    def test_json_object_helpers_preserve_dict_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "payload.json"
            pmcc_memory.write_json_object(path, {"symbol": "US.NVDA", "iv_rank": 61.5})

            self.assertEqual(pmcc_memory.read_json_object(path)["symbol"], "US.NVDA")
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(pmcc_memory.read_json_object(path), {})
            self.assertEqual(pmcc_memory.read_json_object(Path(tmpdir) / "missing.json"), {})
            base = [pmcc.PositionInput("US.NVDA270617C155000", "US.NVDA", 1, cost_price=64.4)]
            pmcc_memory.save_position_memory(path, base, [])
            memory = pmcc_memory.load_position_memory(path)
            self.assertEqual(memory["base_positions"], "US.NVDA270617C155000,1,64.4")

    def test_report_text_writer_preserves_utf8_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.html"
            pmcc_reports.write_report_text(path, "<h1>PMCC 报告</h1>")

            self.assertEqual(path.read_text(encoding="utf-8"), "<h1>PMCC 报告</h1>")
            self.assertEqual(pmcc_reports.format_value(12.345, 2), "12.35")
            self.assertEqual(pmcc_reports.format_value(None, 2, empty="-"), "-")
            self.assertEqual(pmcc_reports.html_text("<NVDA>"), "&lt;NVDA&gt;")
            self.assertEqual(pmcc_reports.html_value(12.345, 1), "12.3")
            self.assertEqual(pmcc_reports.grouped_row_classes([["A"], ["A"], ["B"]], [0]), ["group-row group-0", "group-row group-0", "group-row group-1"])
            self.assertIn("<table>", pmcc_reports.html_table(["Symbol"], [["US.NVDA"]]))
            self.assertEqual(pmcc_reports.explain_trend("FLAT"), "震荡，方向不明显")
            self.assertEqual(pmcc_reports.explain_action("SELL_CALL"), "条件较适合卖出 short call")
            self.assertIn("候选合约 Delta", pmcc_reports.explain_reason("Selected delta 0.32"))
            self.assertEqual(pmcc_reports.clean_sentence_fragment("等待。"), "等待")
            self.assertEqual(pmcc_reports.short_call_risk_light({"roll_action": "ROLL_NOW"}), ("RED", "act now"))
            self.assertEqual(pmcc_reports.symbol_risk_light({"action": "SELL_CALL"}), ("GREEN", "sell candidate"))
            self.assertEqual(pmcc_reports.risk_class("红"), "risk-red")
            self.assertEqual(pmcc_reports.event_risk_class({"attention_events": [{}]}), "event-attention")
            self.assertEqual(pmcc_reports.validation_risk_class({"earnings_check": {"status": "WARN"}}), "validation-warn")
            self.assertEqual(pmcc_reports.format_terminal_table(["A"], [["B"]]), ["A", "-", "B"])
            self.assertEqual(pmcc_reports.format_net_roll_price({"estimated_net_credit": 1.25}), "+1.25")
            self.assertEqual(pmcc_reports.effective_candidate_capacity({"max_new_short_calls": 2}, {"available_to_sell": 1}), 2)
            self.assertIn("PASS", pmcc_reports.format_liquidity_summary({"liquidity": {"ok": True, "spread_pct": 12.3}}))
            self.assertIn("earnings WARN", pmcc_reports.summarize_data_validation({"enabled": True, "price_check": {"status": "OK"}, "earnings_check": {"status": "WARN"}}))
            self.assertIn("historical IV Rank", pmcc_reports.summarize_iv_rank_analysis({"is_true_historical_iv_rank": True, "source": "history", "lookback_count": 10}))
            self.assertIn("IV/HV", pmcc_reports.summarize_iv_environment({"label": "FAVORABLE", "iv_hv_ratio": 1.2}))
            self.assertIn("BLOCKED", pmcc_reports.summarize_event_block({"blocked": True, "blocking_events": [{"type": "earnings", "days_to_event": 5}]}))
            detail_card = pmcc_reports.html_symbol_detail_card(
                {"symbol": "US.NVDA", "summary_cn": "unit <summary>", "candidate_short_call": {"code": "US.NVDA260618C240000"}},
                {"price": 219.51, "trend": "FLAT", "iv_environment": {"label": "FAVORABLE"}},
            )
            self.assertIn("<section class=\"card\">", detail_card)
            self.assertIn("unit &lt;summary&gt;", detail_card)
            self.assertIn("US.NVDA260618C240000", detail_card)
            roll_detail_card = pmcc_reports.html_symbol_detail_card(
                {
                    "symbol": "US.NVDA",
                    "short_call_reviews": [
                        {
                            "code": "US.NVDA260618C240000",
                            "roll_candidates": [
                                {
                                    "code": "US.NVDA260717C250000",
                                    "whole_symbol_roll_pnl": {
                                        "roll_net_cashflow_est": -181.36,
                                        "old_short_realized_pnl_est": -401.3,
                                        "long_leg_unrealized_pnl_est": 2800.0,
                                        "symbol_total_pnl_before_roll_est": 2398.7,
                                        "symbol_total_pnl_after_roll_est": 2398.7,
                                        "after_roll_scenarios": [
                                            {
                                                "spot": 230.0,
                                                "old_short_realized_pnl_est": -401.3,
                                                "long_intrinsic_pnl_est": 1500.0,
                                                "new_short_pnl_at_expiry_est": 820.0,
                                                "symbol_total_pnl_est": 1918.7,
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                },
                {"price": 219.51, "trend": "FLAT"},
            )
            self.assertIn("Roll 整体标的 P/L 估算", roll_detail_card)
            self.assertIn("US.NVDA260717C250000", roll_detail_card)
            self.assertIn("-181.36", roll_detail_card)
            self.assertIn("230.00", roll_detail_card)
            header = pmcc_reports.html_report_header(
                "2026-05-31 12:00:00",
                {"branch": "test<branch>", "version": "abc1234"},
                {"total_base_contracts": 1, "total_short_call_contracts": 2, "total_short_put_contracts": 3},
                4,
            )
            self.assertIn("test&lt;branch&gt;", header)
            self.assertIn("<strong>4</strong>", header)
            css = pmcc_reports.html_report_css()
            self.assertIn(".wrap", css)
            self.assertIn("@media", css)
            self.assertIn("本次持仓确认", pmcc_reports.html_positions_section([["US.NVDA"]], [], [], []))
            self.assertIn("平台分组总览", pmcc_reports.html_overview_section([["Futu", "US.NVDA"]], ["risk-green"]))
            self.assertIn("今日优先事项", pmcc_reports.html_priority_section([["GREEN"]], ["risk-green"]))
            self.assertIn("No short put payoff", pmcc_reports.html_short_put_payoff_section([], [], []))
            self.assertIn("新增卖CALL判断", pmcc_reports.html_sell_section([]))
            self.assertIn("ROLL候选表", pmcc_reports.html_roll_section([]))
            self.assertIn("&quot;symbol&quot;", pmcc_reports.html_raw_json_section('{"symbol": "US.NVDA"}'))
            position_rows = pmcc_reports.build_html_position_rows(
                [{"underlying": "US.NVDA", "code": "US.NVDA270617C155000", "quantity": 1, "strike": 155.0}],
                [
                    {"underlying": "US.NVDA", "code": "US.NVDA260618C240000", "quantity": 1, "strike": 240.0, "option_type": "CALL"},
                    {"underlying": "US.NVDA", "code": "US.NVDA260618P180000", "quantity": 1, "strike": 180.0, "option_type": "PUT"},
                    {"underlying": "US.NVDA", "code": "US.NVDA260618X180000", "quantity": 1, "strike": 180.0, "option_type": "OTHER"},
                ],
            )
            self.assertEqual(position_rows["base_rows"][0][0], "US.NVDA")
            self.assertEqual(position_rows["short_call_rows"][0][1], "US.NVDA260618C240000")
            self.assertEqual(position_rows["short_put_rows"][0][1], "US.NVDA260618P180000")
            self.assertEqual(position_rows["other_short_rows"][0][5], "OTHER")
            recommendation_rows = pmcc_reports.build_html_recommendation_rows(
                {
                    "US.NVDA": {
                        "price": 219.51,
                        "trend": "FLAT",
                        "iv_environment": {"label": "FAVORABLE"},
                        "coverage": {"base_contracts": 1, "short_call_contracts": 1, "available_to_sell": 0},
                    }
                },
                [
                    {
                        "symbol": "US.NVDA",
                        "action": "WAIT",
                        "candidate_short_call": {"code": "US.NVDA260618C240000"},
                        "short_call_reviews": [
                            {
                                "code": "US.NVDA260618C240000",
                                "roll_action": "PLAN_ROLL",
                                "strike": 240.0,
                                "roll_candidates": [{"code": "US.NVDA260717C250000", "strike": 250.0, "estimated_net_credit": 0.2}],
                            }
                        ],
                        "short_put_reviews": [
                            {
                                "code": "US.NVDA260618P180000",
                                "roll_action": "MONITOR",
                                "strike": 180.0,
                                "payoff_scenarios": {
                                    "summary": "static payoff",
                                    "rows": [{"spot_at_expiry": 170.0, "assigned": True, "effective_share_cost": 178.5}],
                                },
                            }
                        ],
                    }
                ],
            )
            self.assertEqual(recommendation_rows["overview_rows"][0][0], "-")
            self.assertEqual(recommendation_rows["overview_rows"][0][1], "US.NVDA")
            self.assertEqual(len(recommendation_rows["priority_rows"]), 2)
            self.assertEqual(recommendation_rows["roll_rows"][0][4], "US.NVDA260717C250000")
            self.assertEqual(recommendation_rows["put_payoff_rows"][0][8], "YES")
            self.assertIn("static payoff", recommendation_rows["put_payoff_summaries"][0])

    def test_runtime_file_protection_check_passes_for_current_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "scripts" / "check_runtime_files.py"
        spec = importlib.util.spec_from_file_location("check_runtime_files", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.missing_gitignore_patterns(repo_root), [])
        self.assertEqual(module.tracked_runtime_files(repo_root), [])

    def test_trade_journal_default_path_is_outside_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        default_path = pmcc.TRADE_JOURNAL_FILE.resolve()

        self.assertFalse(default_path.is_relative_to(repo_root.resolve()))
        self.assertEqual(default_path.name, "pmcc_trade_journal.jsonl")

    def test_report_renderers_preserve_basic_output_shape(self) -> None:
        result = {
            "mode": "pmcc_two_stage",
            "position_source": {
                "source": "unit_test",
                "total_base_contracts": 1,
                "total_short_call_contracts": 1,
                "total_short_put_contracts": 0,
            },
            "recorded_positions": {
                "base_positions": [{"underlying": "US.NVDA", "code": "US.NVDA270617C155000", "quantity": 1}],
                "short_calls": [{"underlying": "US.NVDA", "code": "US.NVDA260618C240000", "quantity": 1}],
            },
            "stage_1_position_analysis": [
                {
                    "symbol": "US.NVDA",
                    "price": 219.51,
                    "coverage": {"base_contracts": 1, "short_call_contracts": 1, "available_to_sell": 0},
                    "iv_environment": {"label": "FAVORABLE"},
                    "data_validation": {"status": "OK"},
                }
            ],
            "stage_2_operation_recommendations": [
                {
                    "symbol": "US.NVDA",
                    "action": "WAIT",
                    "reason": ["unit test"],
                    "candidate_short_call": {"code": "US.NVDA260618C240000", "consider_selling": False},
                    "short_call_reviews": [],
                    "short_put_reviews": [],
                    "summary_cn": "unit test summary",
                }
            ],
        }

        html_report = pmcc.render_html_report(result)
        module_html_report = pmcc_reports.build_html_report(
            result,
            report_version={"branch": "test-branch", "version": "abc1234"},
            generated_at="2026-05-31 12:00:00",
        )
        plain_report = pmcc.format_plain_text_report(result)

        self.assertIn("<!doctype html>", html_report)
        self.assertIn("PMCC", html_report)
        self.assertIn("US.NVDA", html_report)
        self.assertIn("test-branch", module_html_report)
        self.assertIn("abc1234", module_html_report)
        self.assertIn("2026-05-31 12:00:00", module_html_report)
        self.assertIn("US.NVDA", plain_report)
        self.assertIn("unit test", plain_report)
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "pmcc_report.html"
            written_path = pmcc_reports.write_html_report(
                report_path,
                result,
                report_version={"branch": "test-branch", "version": "abc1234"},
                generated_at="2026-05-31 12:00:00",
            )
            self.assertEqual(written_path, report_path)
            self.assertIn("test-branch", report_path.read_text(encoding="utf-8"))

    def test_report_renderers_separate_multi_portfolio_rows(self) -> None:
        result = {
            "mode": "pmcc_two_stage_multi_portfolio",
            "position_source": {
                "source": "unit_test",
                "portfolio_isolation": "broker_account",
                "total_base_contracts": 1,
                "total_short_call_contracts": 1,
                "total_short_put_contracts": 0,
            },
            "recorded_positions": {
                "portfolios": [
                    {
                        "portfolio_id": "FUTU",
                        "portfolio_label": "Futu OpenD",
                        "base_positions": [],
                        "short_calls": [{"underlying": "US.NVDA", "code": "US.NVDA260618C240000", "quantity": 1, "option_type": "CALL"}],
                    },
                    {
                        "portfolio_id": "SCHWAB",
                        "portfolio_label": "Schwab",
                        "base_positions": [{"underlying": "US.NVDA", "code": "US.NVDA270617C155000", "quantity": 1}],
                        "short_calls": [],
                    },
                ],
            },
            "stage_1_position_analysis": [
                {
                    "portfolio_id": "FUTU",
                    "portfolio_label": "Futu OpenD",
                    "symbol": "US.NVDA",
                    "price": 211.14,
                    "coverage": {"base_contracts": 0, "short_call_contracts": 1, "available_to_sell": 0},
                    "iv_environment": {"label": "NEUTRAL"},
                    "data_validation": {"status": "OK"},
                },
                {
                    "portfolio_id": "SCHWAB",
                    "portfolio_label": "Schwab",
                    "symbol": "US.NVDA",
                    "price": 211.14,
                    "coverage": {"base_contracts": 1, "short_call_contracts": 0, "available_to_sell": 1},
                    "iv_environment": {"label": "NEUTRAL"},
                    "data_validation": {"status": "OK"},
                },
            ],
            "stage_2_operation_recommendations": [
                {
                    "portfolio_id": "FUTU",
                    "portfolio_label": "Futu OpenD",
                    "symbol": "US.NVDA",
                    "action": "WAIT",
                    "candidate_short_call": {"consider_selling": False},
                    "short_call_reviews": [],
                    "short_put_reviews": [],
                },
                {
                    "portfolio_id": "SCHWAB",
                    "portfolio_label": "Schwab",
                    "symbol": "US.NVDA",
                    "action": "CONSIDER_SELL",
                    "candidate_short_call": {"consider_selling": True},
                    "short_call_reviews": [],
                    "short_put_reviews": [],
                },
            ],
        }

        html_report = pmcc_reports.build_html_report(result)
        plain_report = pmcc.format_plain_text_report(result)

        self.assertIn("不同平台之间不互相覆盖", html_report)
        self.assertIn("platform-ledger", html_report)
        self.assertIn("同平台内独立判断覆盖", html_report)
        self.assertIn("<h2>Futu OpenD</h2>", html_report)
        self.assertIn("<h2>Schwab</h2>", html_report)
        self.assertIn("Futu OpenD · US.NVDA", html_report)
        self.assertIn("Schwab · US.NVDA", html_report)
        self.assertNotIn("<h2>YELLOW</h2>", html_report)
        self.assertNotIn("<h2>RED</h2>", html_report)
        self.assertIn("Futu OpenD", plain_report)
        self.assertIn("Schwab", plain_report)

    def test_roll_pnl_calculates_whole_symbol_components(self) -> None:
        result = pmcc_roll_pnl.estimate_whole_symbol_roll_pnl(
            {
                "code": "US.NVDA260618C240000",
                "quantity": 2,
                "strike": 240.0,
                "cost_price": 3.00,
                "mark_price": 5.00,
                "commission": 1.30,
            },
            {
                "code": "US.NVDA260717C255000",
                "strike": 255.0,
                "bid_price": 4.10,
                "estimated_buyback_price": 5.00,
                "fees": 0.06,
            },
            {
                "legs": [
                    {
                        "code": "US.NVDA270617C155000",
                        "quantity": 2,
                        "strike": 155.0,
                        "cost_price": 64.0,
                        "mark_price": 78.0,
                    }
                ]
            },
            stock_price=250.0,
            portfolio_identity={"portfolio_id": "FUTU", "symbol": "US.NVDA"},
        )

        self.assertEqual(result["old_short_realized_pnl_est"], -401.3)
        self.assertEqual(result["roll_net_cashflow_est"], -181.36)
        self.assertEqual(result["long_leg_unrealized_pnl_est"], 2800.0)
        self.assertEqual(result["symbol_total_pnl_before_roll_est"], 2398.7)
        self.assertEqual(result["symbol_total_pnl_after_roll_est"], 2398.7)
        self.assertEqual(result["portfolio_identity"]["portfolio_id"], "FUTU")
        self.assertEqual(result["missing_pnl_inputs"], [])
        self.assertTrue(result["after_roll_scenarios"])

    def test_roll_pnl_keeps_total_unavailable_when_long_mark_missing(self) -> None:
        result = pmcc_roll_pnl.estimate_whole_symbol_roll_pnl(
            {"quantity": 1, "strike": 240.0, "cost_price": 3.00, "mark_price": 5.00},
            {"strike": 255.0, "bid_price": 4.10, "estimated_buyback_price": 5.00},
            {"legs": [{"code": "US.NVDA270617C155000", "quantity": 1, "strike": 155.0, "cost_price": 64.0}]},
            stock_price=250.0,
        )

        self.assertEqual(result["old_short_realized_pnl_est"], -200.0)
        self.assertEqual(result["roll_net_cashflow_est"], -90.0)
        self.assertIsNone(result["long_leg_unrealized_pnl_est"])
        self.assertIsNone(result["symbol_total_pnl_after_roll_est"])
        self.assertIn("US.NVDA270617C155000.mark_price", result["missing_pnl_inputs"])

    def test_roll_pnl_blocks_cross_portfolio_identity_mismatch(self) -> None:
        result = pmcc_roll_pnl.estimate_whole_symbol_roll_pnl(
            {"portfolio_id": "FUTU", "symbol": "US.MSFT", "quantity": 1, "strike": 450.0, "cost_price": 3.0, "mark_price": 5.0},
            {"portfolio_id": "FUTU", "symbol": "US.MSFT", "strike": 460.0, "bid_price": 4.0, "estimated_buyback_price": 5.0},
            {
                "portfolio_id": "SCHWAB",
                "symbol": "US.MSFT",
                "legs": [{"code": "US.MSFT270617C350000", "quantity": 1, "strike": 350.0, "cost_price": 80.0, "mark_price": 105.0}],
            },
            stock_price=450.0,
            portfolio_identity={"portfolio_id": "FUTU", "symbol": "US.MSFT"},
        )

        self.assertEqual(result["old_short_realized_pnl_est"], -200.0)
        self.assertIsNone(result["symbol_total_pnl_after_roll_est"])
        self.assertFalse(result["after_roll_scenarios"])
        self.assertIn("long_leg_analysis.portfolio_id_mismatch", result["missing_pnl_inputs"])

    def test_prompt_float_helpers_handle_skip_retry_and_override(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertIsNone(pmcc.prompt_optional_float("IV Rank"))

        with patch("builtins.input", side_effect=["bad", "61.5"]), patch("builtins.print"):
            self.assertEqual(pmcc.prompt_optional_float("IV Rank"), 61.5)

        with patch("builtins.input", return_value=""):
            self.assertEqual(pmcc.prompt_confirm_or_override_float("IV", 45.0), 45.0)

        with patch("builtins.input", return_value="42.25"):
            self.assertEqual(pmcc.prompt_confirm_or_override_float("IV", 45.0), 42.25)

    def test_prompt_trend_helpers_handle_skip_retry_and_override(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertIsNone(pmcc.prompt_optional_trend("Trend"))

        with patch("builtins.input", side_effect=["SIDEWAYS", "flat"]), patch("builtins.print"):
            self.assertEqual(pmcc.prompt_optional_trend("Trend"), "FLAT")

        with patch("builtins.input", return_value=""):
            self.assertEqual(pmcc.prompt_confirm_or_override_trend("Trend", "UP"), "UP")

        with patch("builtins.input", return_value="down"):
            self.assertEqual(pmcc.prompt_confirm_or_override_trend("Trend", "UP"), "DOWN")

    def test_interaction_module_prompt_helpers_match_entry_wrappers(self) -> None:
        with patch("builtins.input", side_effect=["", "value"]), patch("builtins.print"):
            self.assertEqual(pmcc_interaction.prompt_required_text("Name"), "value")

        with patch("builtins.input", side_effect=["bad", "12.5"]), patch("builtins.print"):
            self.assertEqual(pmcc_interaction.prompt_optional_float("IV"), 12.5)

        with patch("builtins.input", side_effect=["bad", "up"]), patch("builtins.print"):
            self.assertEqual(pmcc_interaction.prompt_optional_trend("Trend"), "UP")

    def test_parse_positions_input_rejects_invalid_entry_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "Cannot parse entry"):
            pmcc.parse_positions_input("US.NVDA270617C155000")

    def test_positions_module_preserves_parse_record_and_group_helpers(self) -> None:
        positions = pmcc_positions.parse_positions_input("US.NVDA270617C155000,2,64.4;US.MSFT270617C330000,1")
        full_width_positions = pmcc_positions.parse_positions_input("US.NVDA270617C155000，2，64.4；US.MSFT270617C330000，1")
        records = [pmcc_positions.position_to_record(item) for item in positions]
        restored = pmcc_positions.positions_from_records(records)
        grouped = pmcc_positions.group_positions_by_underlying(restored)

        self.assertEqual(positions[0].underlying, "US.NVDA")
        self.assertEqual(full_width_positions[0].cost_price, 64.4)
        self.assertEqual(full_width_positions[1].quantity, 1)
        self.assertEqual(positions[0].strike, 155.0)
        self.assertEqual(positions[0].option_type, "CALL")
        self.assertEqual(pmcc_positions.positions_to_compact_text(positions), "US.NVDA270617C155000,2,64.4;US.MSFT270617C330000,1")
        self.assertEqual(len(grouped["US.NVDA"]), 1)
        self.assertEqual(len(grouped["US.MSFT"]), 1)

    def test_trade_journal_normalizes_and_validates_manual_event(self) -> None:
        event = pmcc_trade_journal.normalize_trade_event(
            {
                "event_date": "2026-05-26",
                "broker": "schwab",
                "symbol": "us.nvda",
                "strategy": "pmcc",
                "event_type": "open_short_call",
                "option_code": "US.NVDA260618C240000",
                "side": "sell_to_open",
                "quantity": "3",
                "price": "1.25",
                "source": "manual_csv",
                "source_reference": "tos-row-12",
                "confidence": "needs_review",
            }
        )

        self.assertEqual(event["broker"], "SCHWAB")
        self.assertEqual(event["underlying"], "US.NVDA")
        self.assertEqual(event["option_code"], "US.NVDA260618C240000")
        self.assertEqual(event["broker_option_symbol"], "US.NVDA260618C240000")
        self.assertEqual(event["canonical_option_key"], "US.NVDA260618C240000")
        self.assertEqual(event["expiry"], "2026-06-18")
        self.assertEqual(event["strike"], 240.0)
        self.assertEqual(event["option_type"], "CALL")
        self.assertEqual(pmcc_trade_journal.validate_trade_event(event), [])

    def test_trade_journal_preserves_schwab_source_symbol_and_uses_canonical_key(self) -> None:
        event = pmcc_trade_journal.normalize_trade_event(
            {
                "event_date": "2026-05-26",
                "broker": "SCHWAB",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "OPEN_SHORT_CALL",
                "option_code": "US.NVDA 260618 C240000",
                "side": "SELL_TO_OPEN",
                "quantity": 1,
                "price": 1.25,
                "source": "schwab_export",
                "source_reference": "order-1",
                "confidence": "needs_review",
            }
        )

        self.assertEqual(event["option_code"], "US.NVDA 260618 C240000")
        self.assertEqual(event["broker_option_symbol"], "US.NVDA 260618 C240000")
        self.assertEqual(event["canonical_option_key"], "US.NVDA260618C240000")
        self.assertEqual(pmcc_trade_journal.validate_trade_event(event), [])

    def test_trade_journal_flags_suspicious_records_and_appends_jsonl(self) -> None:
        bad_event = {
            "event_date": "2026-05-26",
            "broker": "SCHWAB",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "CLOSE_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "SELL_TO_OPEN",
            "quantity": 1,
            "price": 0.55,
            "source": "manual_cli",
            "confidence": "needs_review",
        }

        issues = pmcc_trade_journal.validate_trade_event(bad_event)
        self.assertIn("side should be BUY_TO_CLOSE for CLOSE_SHORT_CALL", issues)
        self.assertIn("source_reference is required for SCHWAB trades", issues)

        good_event = dict(bad_event, side="BUY_TO_CLOSE", source_reference="manual-confirmed")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            saved = pmcc_trade_journal.append_trade_event(path, good_event)
            loaded = pmcc_trade_journal.read_trade_events(path)

        self.assertEqual(saved["side"], "BUY_TO_CLOSE")
        self.assertEqual(loaded, [saved])

    def test_trade_journal_flags_close_without_known_open_position(self) -> None:
        close_event = {
            "event_date": "2026-05-26",
            "broker": "SCHWAB",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "CLOSE_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "BUY_TO_CLOSE",
            "quantity": 1,
            "price": 0.55,
            "source": "manual_cli",
            "source_reference": "manual-confirmed",
            "confidence": "needs_review",
        }
        open_event = dict(close_event, event_type="OPEN_SHORT_CALL", side="SELL_TO_OPEN", price=1.2)

        self.assertEqual(
            pmcc_trade_journal.validate_trade_event_against_journal(close_event, []),
            ["close without known open position"],
        )
        self.assertEqual(
            pmcc_trade_journal.validate_trade_event_against_journal(close_event, [open_event]),
            [],
        )

    def test_trade_journal_parses_paste_friendly_text(self) -> None:
        event = pmcc_trade_journal.parse_trade_event_text(
            "event_date=2026-05-26; broker=SCHWAB; symbol=US.NVDA; strategy=PMCC; "
            "event_type=CLOSE_SHORT_CALL; option_code=US.NVDA260618C240000; side=BUY_TO_CLOSE; "
            "quantity=3; price=0.55; source=manual_cli; source_reference=manual-confirmed; "
            "confidence=needs_review; notes=Closed after 50 pct capture"
        )

        self.assertEqual(event["quantity"], 3)
        self.assertEqual(event["price"], 0.55)
        self.assertEqual(event["expiry"], "2026-06-18")
        self.assertEqual(pmcc_trade_journal.validate_trade_event(event), [])

    def test_trade_journal_cli_records_event_after_confirmation(self) -> None:
        raw_event = (
            "event_date=2026-05-26; broker=SCHWAB; symbol=US.NVDA; strategy=PMCC; "
            "event_type=CLOSE_SHORT_CALL; option_code=US.NVDA260618C240000; side=BUY_TO_CLOSE; "
            "quantity=3; price=0.55; source=manual_cli; source_reference=manual-confirmed; "
            "confidence=needs_review"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            with patch("builtins.input", return_value="y"), patch("builtins.print"):
                result = pmcc.record_trade_journal_event(raw_event, path)

            self.assertTrue(result["saved"])
            self.assertEqual(pmcc_trade_journal.read_trade_events(path), [result["event"]])

    def test_trade_journal_cli_does_not_record_without_confirmation(self) -> None:
        raw_event = (
            "event_date=2026-05-26; broker=SCHWAB; symbol=US.NVDA; strategy=PMCC; "
            "event_type=CLOSE_SHORT_CALL; option_code=US.NVDA260618C240000; side=BUY_TO_CLOSE; "
            "quantity=3; price=0.55; source=manual_cli; source_reference=manual-confirmed; "
            "confidence=needs_review"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            with patch("builtins.input", return_value="n"), patch("builtins.print"):
                result = pmcc.record_trade_journal_event(raw_event, path)

            self.assertFalse(result["saved"])
            self.assertFalse(path.exists())
            self.assertEqual(pmcc_trade_journal.read_trade_events(path), [])

    def test_trade_journal_cli_imports_valid_schwab_csv_rows_after_confirmation(self) -> None:
        csv_text = (
            "Date,Action,Symbol,Qty,Price,Account,Order ID\n"
            "2026-05-26,BTO,US.NVDA260618C240000,3,0.55,IRA,order-1\n"
            "2026-05-26,STO,BADSYMBOL,1,1.20,IRA,order-2\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "trades.csv"
            journal_path = Path(tmpdir) / "journal.jsonl"
            csv_path.write_text(csv_text, encoding="utf-8")
            with patch("builtins.input", return_value="y"), patch("builtins.print"):
                result = pmcc.import_schwab_trade_csv(csv_path, journal_path)

            loaded = pmcc_trade_journal.read_trade_events(journal_path)

        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["source"], "schwab_export")
        self.assertTrue(loaded[0]["trade_id"].startswith("trade-"))

    def test_trade_journal_cli_does_not_import_schwab_csv_without_confirmation(self) -> None:
        csv_text = (
            "Date,Action,Symbol,Qty,Price,Account,Order ID\n"
            "2026-05-26,BTO,US.NVDA260618C240000,3,0.55,IRA,order-1\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "trades.csv"
            journal_path = Path(tmpdir) / "journal.jsonl"
            csv_path.write_text(csv_text, encoding="utf-8")
            with patch("builtins.input", return_value="n"), patch("builtins.print"):
                result = pmcc.import_schwab_trade_csv(csv_path, journal_path)

            self.assertEqual(result["saved"], 0)
            self.assertEqual(result["invalid"], 0)
            self.assertFalse(journal_path.exists())
            self.assertEqual(pmcc_trade_journal.read_trade_events(journal_path), [])

    def test_trade_journal_groups_pmcc_short_call_cycle(self) -> None:
        original_open_event = pmcc_trade_journal.normalize_trade_event(
            {
                "event_date": "2026-05-20",
                "broker": "SCHWAB",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "OPEN_SHORT_CALL",
                "option_code": "US.NVDA260618C240000",
                "side": "SELL_TO_OPEN",
                "quantity": 3,
                "price": 1.0,
                "source": "manual_cli",
                "source_reference": "original-open-row",
                "confidence": "needs_review",
            }
        )
        close_event = pmcc_trade_journal.normalize_trade_event(
            {
                "event_date": "2026-05-26",
                "broker": "SCHWAB",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "CLOSE_SHORT_CALL",
                "option_code": "US.NVDA260618C240000",
                "side": "BUY_TO_CLOSE",
                "quantity": 3,
                "price": 0.55,
                "profit_capture_pct": 50,
                "source": "manual_cli",
                "source_reference": "close-row",
                "confidence": "needs_review",
                "reason_tags": ["theta_decay", "probability_win"],
                "notes": "Closed after theta decay",
            }
        )
        open_event = pmcc_trade_journal.normalize_trade_event(
            {
                "event_date": "2026-05-26",
                "broker": "SCHWAB",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "OPEN_SHORT_CALL",
                "option_code": "US.NVDA260717C250000",
                "side": "SELL_TO_OPEN",
                "quantity": 3,
                "price": 1.2,
                "source": "manual_cli",
                "source_reference": "open-row",
                "confidence": "needs_review",
                "reason": "program_recommendation",
                "notes": "Opened replacement short calls",
            }
        )

        cycles = pmcc_trade_journal.build_pmcc_short_call_cycles([original_open_event, open_event, close_event])

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["cycle_type"], "PMCC_SHORT_CALL_ROLL")
        self.assertEqual(cycles[0]["quantity_closed"], 3)
        self.assertEqual(cycles[0]["quantity_opened"], 3)
        self.assertEqual(cycles[0]["realized_pnl"], 135.0)
        self.assertEqual(cycles[0]["profit_capture_pct"], 50.0)
        self.assertEqual(cycles[0]["reason_tags"], ["theta_decay", "probability_win", "program_recommendation"])
        self.assertIn("theta decay", cycles[0]["notes"])

    def test_trade_journal_calculates_realized_pnl_from_open_close(self) -> None:
        open_event = {
            "broker": "SCHWAB",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "OPEN_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "SELL_TO_OPEN",
            "quantity": 2,
            "price": 1.25,
            "commission": 0.65,
        }
        close_event = dict(open_event, event_type="CLOSE_SHORT_CALL", side="BUY_TO_CLOSE", price=0.55, fees=0.03)

        realized = pmcc_trade_journal.calculate_realized_pnl_from_open_close(open_event, close_event)

        self.assertEqual(realized, 138.67)

    def test_trade_journal_matches_realized_pnl_by_same_option_contract(self) -> None:
        old_open = {
            "event_date": "2026-05-20",
            "broker": "SCHWAB",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "OPEN_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "SELL_TO_OPEN",
            "quantity": 1,
            "price": 1.25,
            "source": "manual_cli",
            "source_reference": "open-old",
            "confidence": "needs_review",
        }
        replacement_open = dict(old_open, event_date="2026-05-26", option_code="US.NVDA260717C250000", source_reference="open-new")
        close = dict(old_open, event_date="2026-05-26", event_type="CLOSE_SHORT_CALL", side="BUY_TO_CLOSE", price=0.55, source_reference="close-old")

        matches = pmcc_trade_journal.build_realized_pnl_matches([old_open, replacement_open, close])

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["open_event"]["option_code"], "US.NVDA260618C240000")
        self.assertEqual(matches[0]["close_event"]["option_code"], "US.NVDA260618C240000")
        self.assertEqual(matches[0]["realized_pnl"], 70.0)

    def test_trade_journal_matches_partial_close_fifo_and_prorates_costs(self) -> None:
        old_open = {
            "event_date": "2026-05-20",
            "broker": "SCHWAB",
            "account": "MAIN",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "OPEN_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "SELL_TO_OPEN",
            "quantity": 3,
            "price": 1.25,
            "commission": 0.90,
            "source": "manual_cli",
            "source_reference": "open-old",
            "confidence": "needs_review",
        }
        close_one = dict(
            old_open,
            event_date="2026-05-26",
            event_type="CLOSE_SHORT_CALL",
            side="BUY_TO_CLOSE",
            quantity=1,
            price=0.55,
            commission=0.0,
            fees=0.03,
            source_reference="close-one",
        )
        close_two = dict(close_one, quantity=2, source_reference="close-two")

        matches = pmcc_trade_journal.build_realized_pnl_matches([old_open, close_one, close_two])

        self.assertEqual([item["matched_quantity"] for item in matches], [1, 2])
        self.assertEqual(matches[0]["open_remaining_quantity"], 2)
        self.assertEqual(matches[1]["open_remaining_quantity"], 0)
        self.assertEqual(matches[0]["realized_pnl"], 69.67)
        self.assertEqual(matches[1]["realized_pnl"], 139.37)

    def test_trade_journal_suggests_missing_futu_open_event_drafts(self) -> None:
        positions = [
            pmcc.PositionInput(
                raw_code="US.NVDA260618C240000",
                underlying="US.NVDA",
                quantity=1,
                strike=240.0,
                expiry="2026-06-18",
                option_type="CALL",
                cost_price=1.25,
            ),
            pmcc.PositionInput(
                raw_code="US.NVDA260717C250000",
                underlying="US.NVDA",
                quantity=1,
                strike=250.0,
                expiry="2026-07-17",
                option_type="CALL",
                cost_price=None,
            ),
        ]
        existing_events = [
            {
                "event_date": "2026-05-25",
                "broker": "FUTU",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "OPEN_SHORT_CALL",
                "option_code": "US.NVDA260618C240000",
                "side": "SELL_TO_OPEN",
                "quantity": 1,
                "price": 1.25,
                "source": "futu_opend",
                "confidence": "confirmed",
            }
        ]

        drafts = pmcc_trade_journal.suggest_futu_open_event_drafts(positions, existing_events, "2026-05-26")

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["event"]["option_code"], "US.NVDA260717C250000")
        self.assertEqual(drafts[0]["event"]["broker"], "FUTU")
        self.assertIn("missing price", drafts[0]["issues"])

    def test_trade_journal_suggests_futu_short_put_draft_as_csp(self) -> None:
        positions = [
            pmcc.PositionInput(
                raw_code="US.NVDA260717P180000",
                underlying="US.NVDA",
                quantity=1,
                strike=180.0,
                expiry="2026-07-17",
                option_type="PUT",
                cost_price=2.15,
            )
        ]

        drafts = pmcc_trade_journal.suggest_futu_open_event_drafts(positions, [], "2026-05-26", account="FUTU-MAIN")

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["issues"], [])
        self.assertEqual(drafts[0]["event"]["strategy"], "CSP")
        self.assertEqual(drafts[0]["event"]["event_type"], "OPEN_SHORT_PUT")
        self.assertEqual(drafts[0]["event"]["side"], "SELL_TO_OPEN")
        self.assertEqual(drafts[0]["event"]["confidence"], "needs_review")
        self.assertEqual(drafts[0]["event"]["account"], "FUTU-MAIN")

    def test_trade_journal_parses_schwab_csv_drafts_for_review(self) -> None:
        csv_text = (
            "Date,Action,Symbol,Qty,Price,Account,Order ID\n"
            "2026-05-26,BTO,US.NVDA260618C240000,3,0.55,IRA,order-1\n"
            "2026-05-26,STO,BADSYMBOL,1,1.20,IRA,order-2\n"
        )

        drafts = pmcc_trade_journal.parse_schwab_trade_csv_text(csv_text)

        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[0]["event"]["broker"], "SCHWAB")
        self.assertEqual(drafts[0]["event"]["event_type"], "OPEN_LONG_CALL")
        self.assertEqual(drafts[0]["event"]["source"], "schwab_export")
        self.assertEqual(drafts[0]["event"]["confidence"], "needs_review")
        self.assertEqual(drafts[0]["issues"], [])
        self.assertIn("option_code cannot be parsed", drafts[1]["issues"])

    def test_trade_journal_parses_schwab_csv_signed_currency_values(self) -> None:
        csv_text = (
            "Date,Action,Symbol,Qty,Price,Commission,Fees,Account,Order ID\n"
            "2026-05-26,BTC,US.NVDA 260618 C240000,-3,$0.55,($0.65),$0.03,IRA,order-1\n"
        )

        drafts = pmcc_trade_journal.parse_schwab_trade_csv_text(csv_text)

        self.assertEqual(drafts[0]["event"]["quantity"], 3)
        self.assertEqual(drafts[0]["event"]["price"], 0.55)
        self.assertEqual(drafts[0]["event"]["commission"], 0.65)
        self.assertEqual(drafts[0]["event"]["fees"], 0.03)
        self.assertEqual(drafts[0]["event"]["option_code"], "US.NVDA 260618 C240000")
        self.assertEqual(drafts[0]["event"]["canonical_option_key"], "US.NVDA260618C240000")
        self.assertEqual(drafts[0]["issues"], [])

    def test_trade_journal_builds_obsidian_note_draft(self) -> None:
        event = pmcc_trade_journal.normalize_trade_event(
            {
                "trade_id": "trade-1",
                "event_date": "2026-05-26",
                "broker": "SCHWAB",
                "symbol": "US.NVDA",
                "strategy": "PMCC",
                "event_type": "CLOSE_SHORT_CALL",
                "option_code": "US.NVDA260618C240000",
                "side": "BUY_TO_CLOSE",
                "quantity": 3,
                "price": 0.55,
                "source": "manual_cli",
                "source_reference": "manual-confirmed",
                "confidence": "needs_review",
            }
        )

        note = pmcc_trade_journal.build_obsidian_trade_note([event], "Theta decay worked; avoid over-crediting direction.")

        self.assertIn("# Trade Review - 2026-05-26 - US.NVDA", note)
        self.assertIn("CLOSE_SHORT_CALL", note)
        self.assertIn("trade_id: `trade-1`", note)
        self.assertIn("Theta decay worked", note)

    def test_trade_journal_cli_renders_obsidian_note_from_journal(self) -> None:
        event = {
            "event_date": "2026-05-26",
            "broker": "SCHWAB",
            "symbol": "US.NVDA",
            "strategy": "PMCC",
            "event_type": "CLOSE_SHORT_CALL",
            "option_code": "US.NVDA260618C240000",
            "side": "BUY_TO_CLOSE",
            "quantity": 3,
            "price": 0.55,
            "source": "manual_cli",
            "source_reference": "manual-confirmed",
            "confidence": "needs_review",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            pmcc_trade_journal.append_trade_event(path, event)
            note = pmcc.render_trade_journal_obsidian_note(path, "Review the roll timing.")

        self.assertIn("Trade Review", note)
        self.assertIn("CLOSE_SHORT_CALL", note)
        self.assertIn("trade_id: `trade-", note)
        self.assertIn("Review the roll timing.", note)

    def test_parse_positions_input_normalizes_whitespace_inside_option_codes(self) -> None:
        positions = pmcc_positions.parse_positions_input("US.NVDA    260618C235000,1,249;US.NVDA260618C230000,1,464")
        grouped = pmcc_positions.group_positions_by_underlying(positions)

        self.assertEqual(positions[0].raw_code, "US.NVDA260618C235000")
        self.assertEqual(positions[0].underlying, "US.NVDA")
        self.assertEqual(len(grouped["US.NVDA"]), 2)

    def test_prompt_optional_positions_handles_skip_and_retry(self) -> None:
        with patch("builtins.input", return_value=""), patch("builtins.print"):
            self.assertEqual(pmcc.prompt_optional_positions("short calls"), [])

        with patch("builtins.input", side_effect=["bad-entry", "US.NVDA260618C240000,1,3.74"]), patch(
            "builtins.print"
        ):
            positions = pmcc.prompt_optional_positions("short calls")

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].raw_code, "US.NVDA260618C240000")

    def test_interaction_module_position_prompts_parse_memory_and_optional_inputs(self) -> None:
        with patch("builtins.input", return_value=""), patch("builtins.print"):
            positions = pmcc_interaction.prompt_positions_with_memory("base positions", "US.NVDA260618C240000,1,3.74")

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].cost_price, 3.74)

        with patch("builtins.input", side_effect=["bad-entry", "US.NVDA260618C240000,1"]), patch("builtins.print"):
            optional_positions = pmcc_interaction.prompt_optional_positions("short calls")

        self.assertEqual(len(optional_positions), 1)
        self.assertEqual(optional_positions[0].quantity, 1)

    def test_interaction_module_collects_position_workflows(self) -> None:
        memory = {
            "base_positions": "US.NVDA270617C155000,1,64.4",
            "short_calls": "US.NVDA260618C240000,1,3.74",
        }
        with patch("builtins.input", side_effect=["", ""]), patch("builtins.print"):
            base_positions, short_calls, metadata = pmcc_interaction.collect_positions_interactive(memory)

        self.assertEqual(len(base_positions), 1)
        self.assertEqual(len(short_calls), 1)
        self.assertEqual(metadata["source"], "interactive")
        self.assertEqual(metadata["base_contracts"], 1)

        with patch("builtins.input", side_effect=["US.NVDA270617C155000,1,64.4", ""]), patch("builtins.print"):
            external_base, external_short, external_metadata = pmcc_interaction.collect_external_positions_interactive()

        self.assertEqual(len(external_base), 1)
        self.assertEqual(external_short, [])
        self.assertEqual(external_metadata["source"], "external_interactive")

    def test_interaction_module_prompts_short_call_manual_metrics(self) -> None:
        short_call = pmcc.PositionInput("US.NVDA260618C240000", "US.NVDA", 1)
        with patch("builtins.input", side_effect=["0.31", "50", "43.7"]), patch("builtins.print"):
            metrics = pmcc_interaction.prompt_short_call_manual_metrics(short_call)

        self.assertEqual(metrics["delta"], 0.31)
        self.assertEqual(metrics["profit_capture_pct"], 50.0)
        self.assertEqual(metrics["iv"], 43.7)

    def test_interaction_module_collects_iv_rank_overrides(self) -> None:
        memory = {"US.NVDA": {"iv_rank": 55.0, "updated_at": "old", "source": "unit_test"}}
        with patch("builtins.input", side_effect=["", "61.5"]), patch("builtins.print"):
            overrides, updated_memory, metadata, changed = pmcc_interaction.collect_iv_rank_overrides_for_symbols(
                ["US.NVDA", "US.MSFT", "US.AAPL"],
                {},
                memory,
                "memory.json",
                lambda symbol: 70.0 if symbol == "US.AAPL" else None,
                "2026-05-31T12:00:00",
            )

        self.assertTrue(changed)
        self.assertEqual(overrides["US.NVDA"], 55.0)
        self.assertEqual(overrides["US.MSFT"], 61.5)
        self.assertEqual(overrides["US.AAPL"], 70.0)
        self.assertEqual(updated_memory["US.MSFT"]["updated_at"], "2026-05-31T12:00:00")
        self.assertEqual(metadata["symbols"]["US.NVDA"]["source"], "local_memory")
        self.assertEqual(metadata["symbols"]["US.AAPL"]["source"], "cli_override")

    def test_collect_external_positions_from_args_returns_metadata(self) -> None:
        base_positions, short_calls, metadata = pmcc.collect_external_positions_from_args(
            "US.MSFT270617C330000,1,102.01",
            "US.MSFT260618C445000,1,4.19",
        )

        self.assertEqual(base_positions[0].raw_code, "US.MSFT270617C330000")
        self.assertEqual(short_calls[0].raw_code, "US.MSFT260618C445000")
        self.assertEqual(metadata["source"], "external_args")
        self.assertEqual(metadata["base_contracts"], 1)
        self.assertEqual(metadata["short_call_contracts"], 1)

    def test_merge_positions_by_code_combines_same_contract_across_sources(self) -> None:
        futu_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,9.60")
        external_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,5.20")

        merged, metadata = pmcc.merge_positions_by_code(("opend", futu_short), ("external", external_short))

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].raw_code, "US.MSFT260702C460000")
        self.assertEqual(merged[0].quantity, 2)
        self.assertAlmostEqual(merged[0].cost_price, 7.40)
        self.assertEqual(metadata["unique_contract_codes"], 1)
        self.assertEqual(metadata["combined_duplicates"][0]["quantity"], 2)

    def test_merge_positions_by_code_uses_opend_over_memory_snapshot(self) -> None:
        memory_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,2,9.60")
        futu_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,9.60")

        merged, metadata = pmcc.merge_positions_by_code(("memory", memory_short), ("opend", futu_short))

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].raw_code, "US.MSFT260702C460000")
        self.assertEqual(merged[0].quantity, 1)
        self.assertEqual(metadata["replacements"][0]["replacement_source"], "opend")

    def test_merge_positions_by_code_combines_external_memory_with_opend(self) -> None:
        memory_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,5.20")
        futu_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,9.60")

        merged, metadata = pmcc.merge_positions_by_code(("memory_external", memory_short), ("opend", futu_short))

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].raw_code, "US.MSFT260702C460000")
        self.assertEqual(merged[0].quantity, 2)
        self.assertAlmostEqual(merged[0].cost_price, 7.40)
        self.assertEqual(metadata["combined_duplicates"][0]["added_source"], "opend")

    def test_schwab_position_changes_add_and_remove_without_touching_futu(self) -> None:
        schwab_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,5.20")
        add_short = pmcc_positions.parse_positions_input("US.MSFT260618C460000,1,2.20")
        remove_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1")

        updated_base, updated_short, metadata = pmcc.apply_schwab_position_changes(
            [],
            schwab_short,
            [],
            add_short,
            [],
            remove_short,
        )

        self.assertEqual(updated_base, [])
        self.assertEqual(len(updated_short), 1)
        self.assertEqual(updated_short[0].raw_code, "US.MSFT260618C460000")
        self.assertEqual(updated_short[0].quantity, 1)
        self.assertEqual(metadata["total_short_contracts"], 1)
        self.assertEqual(metadata["unmatched_short_removals"], [])

    def test_combine_positions_by_code_sums_futu_and_schwab_same_contract(self) -> None:
        futu_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,9.60")
        schwab_short = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,5.20")

        combined = pmcc.combine_positions_by_code(futu_short, schwab_short)

        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0].raw_code, "US.MSFT260702C460000")
        self.assertEqual(combined[0].quantity, 2)
        self.assertAlmostEqual(combined[0].cost_price, 7.40)

    def test_futu_position_diff_reports_snapshot_changes(self) -> None:
        previous = pmcc_positions.parse_positions_input("US.MSFT260702C460000,1,9.60")
        current = pmcc_positions.parse_positions_input("US.MSFT260702C460000,2,9.60;US.MSFT260618C450000,1,3.05")

        diff = pmcc.diff_positions_by_code(previous, current)

        self.assertTrue(diff["changed"])
        self.assertEqual(diff["opened_or_increased"][0]["code"], "US.MSFT260702C460000")
        self.assertEqual(diff["opened_or_increased"][0]["quantity_change"], 1)
        self.assertEqual(diff["opened_or_increased"][1]["code"], "US.MSFT260618C450000")

    def test_leaps_coverage_slots_count_multi_quantity_short_call(self) -> None:
        long_leg_analysis = {
            "coverage_slots": [
                {"minimum_safe_short_strike": 430.0},
                {"minimum_safe_short_strike": 440.0},
                {"minimum_safe_short_strike": 450.0},
            ]
        }
        short_calls = pmcc_positions.parse_positions_input("US.MSFT260702C460000,2,9.60")

        result = pmcc.build_leaps_coverage_slot_analysis(long_leg_analysis, short_calls, 460.0)

        self.assertEqual(result["total_slots"], 3)
        self.assertEqual(result["occupied_slots"], 2)
        self.assertEqual(result["eligible_new_short_call_slots"], 1)

    def test_parse_tos_position_statement_imports_schwab_snapshot(self) -> None:
        raw = "\n".join(
            [
                "Equities and Equity Options",
                "Instrument,Qty,Days,Trade Price,Mark",
                "MSFT,,,,",
                "100 18 JUN 26 445 CALL,0,20,.00,15.725",
                "100 (Weeklys) 2 JUL 26 460 CALL,-1,34,9.62,12.40",
                "100 17 JUN 27 330 CALL,+1,384,102.00,142.425",
                "NVDA,,,,",
                "100 18 JUN 26 205 PUT,-1,20,2.91,5.10",
                "100 15 JAN 27 140 CALL,+2,231,63.50,79.70",
            ]
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as handle:
            handle.write(raw)
            path = Path(handle.name)

        try:
            bases, shorts, metadata = pmcc.parse_tos_position_statement(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual([(item.raw_code, item.quantity) for item in bases], [
            ("US.MSFT270617C330000", 1),
            ("US.NVDA270115C140000", 2),
        ])
        self.assertEqual([(item.raw_code, item.quantity) for item in shorts], [
            ("US.MSFT260702C460000", 1),
            ("US.NVDA260618P205000", 1),
        ])
        self.assertEqual(metadata["base_contracts"], 3)
        self.assertEqual(metadata["short_contracts"], 2)
        self.assertEqual(metadata["skipped"][0]["reason"], "zero_or_missing_quantity")

    def test_option_data_quality_blocks_missing_delta_and_iv(self) -> None:
        enriched = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C232500",
                    "delta": pd.NA,
                    "implied_volatility": pd.NA,
                    "bid_price": 4.65,
                    "ask_price": 4.75,
                }
            ]
        )

        quality = pmcc.build_option_data_quality("US.NVDA", enriched)

        self.assertEqual(quality["status"], "BLOCKED")
        self.assertIn("Futu returned no usable option Delta values.", quality["issues"])
        self.assertIn("Futu returned no usable option implied volatility values.", quality["issues"])
        with self.assertRaisesRegex(pmcc.DataQualityError, "DATA_QUALITY_BLOCKED for US.NVDA"):
            pmcc.require_option_data_quality("US.NVDA", enriched)

    def test_iv_environment_favorable_with_mid_iv_rank_and_iv_over_hv(self) -> None:
        option = pd.Series({"implied_volatility": 39.1})

        result = pmcc.build_iv_environment(45.0, 35.0, 60.0, 60.0, option)

        self.assertEqual(result["label"], "FAVORABLE")
        self.assertEqual(result["iv_hv_ratio"], 1.286)
        self.assertIn("IV Rank is mid-range", result["notes"])

    def test_candidate_structure_preserves_expected_output_shape(self) -> None:
        option = pd.Series(
            {
                "delta": 0.32,
                "days_to_expiry": 27,
                "otm_pct": 0.06,
                "liquidity_ok": True,
                "liquidity_reasons": [],
                "bid_ask_spread_pct": 2.13,
                "last_mid_deviation_pct": 0.21,
                "volume": 798,
                "open_interest": 846,
            }
        )

        result = pmcc.evaluate_candidate_structure(option, pmcc.DEFAULT_CONFIG)

        self.assertTrue(result["structure_ok"])
        self.assertTrue(result["checks"]["delta_in_target_range"])
        self.assertTrue(result["liquidity"]["ok"])
        self.assertEqual(result["liquidity"]["open_interest"], 846)

    def test_option_scoring_prefers_target_dte_and_moneyness(self) -> None:
        config = pmcc.DEFAULT_CONFIG

        self.assertGreater(pmcc.score_dte(26, config), pmcc.score_dte(5, config))
        self.assertGreater(pmcc.score_dte(26, config), pmcc.score_dte(70, config))
        self.assertGreater(pmcc.score_moneyness(0.03), pmcc.score_moneyness(-0.01))
        self.assertGreater(pmcc.score_moneyness(0.03), pmcc.score_moneyness(0.20))

    def test_liquidity_assessment_flags_wide_spread_and_thin_interest(self) -> None:
        option = pd.Series(
            {
                "bid_price": 1.00,
                "ask_price": 1.60,
                "last_price": 1.30,
                "volume": 3,
                "open_interest": 20,
            }
        )

        result = pmcc.assess_option_liquidity(option, pmcc.DEFAULT_CONFIG)

        self.assertFalse(result["ok"])
        self.assertEqual(result["spread_pct"], 46.15)
        self.assertTrue(any("bid/ask spread" in reason for reason in result["reasons"]))
        self.assertTrue(any("open interest" in reason for reason in result["reasons"]))
        self.assertTrue(any("volume" in reason for reason in result["reasons"]))

    def test_select_option_prefers_liquid_target_delta_candidate(self) -> None:
        options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C232500",
                    "strike_price": 232.5,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 12.99,
                    "is_otm": True,
                    "otm_pct": 0.06,
                    "delta": 0.32,
                    "bid_price": 4.65,
                    "ask_price": 4.75,
                    "last_price": 4.70,
                    "volume": 798,
                    "open_interest": 846,
                },
                {
                    "code": "US.NVDA260618C228000",
                    "strike_price": 228.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 8.49,
                    "is_otm": True,
                    "otm_pct": 0.039,
                    "delta": 0.33,
                    "bid_price": 1.00,
                    "ask_price": 1.60,
                    "last_price": 1.30,
                    "volume": 2,
                    "open_interest": 15,
                },
            ]
        )

        scored = pmcc.score_options(options, pmcc.DEFAULT_CONFIG)
        selected = pmcc.select_option(options, pmcc.DEFAULT_CONFIG)

        self.assertIn("selection_score", scored.columns)
        self.assertTrue(bool(scored.loc[scored["code"] == "US.NVDA260618C232500", "liquidity_ok"].iloc[0]))
        self.assertFalse(bool(scored.loc[scored["code"] == "US.NVDA260618C228000", "liquidity_ok"].iloc[0]))
        self.assertEqual(selected["code"], "US.NVDA260618C232500")

    def test_mcmillan_safe_alternative_requires_cost_line_and_otm_call(self) -> None:
        options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C40000",
                    "strike_price": 40.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 23,
                    "distance_from_spot": -177.0,
                    "is_otm": False,
                    "otm_pct": -0.816,
                    "delta": 0.0,
                    "bid_price": 177.0,
                    "ask_price": 177.2,
                    "last_price": 177.1,
                    "volume": 200,
                    "open_interest": 1000,
                },
                {
                    "code": "US.NVDA260618C230000",
                    "strike_price": 230.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 23,
                    "distance_from_spot": 13.0,
                    "is_otm": True,
                    "otm_pct": 0.06,
                    "delta": 0.30,
                    "bid_price": 4.5,
                    "ask_price": 4.6,
                    "last_price": 4.55,
                    "volume": 200,
                    "open_interest": 1000,
                },
            ]
        )

        self.assertTrue(pmcc.find_mcmillan_safe_alternative(options, pmcc.DEFAULT_CONFIG, None).empty)
        alternative = pmcc.find_mcmillan_safe_alternative(options, pmcc.DEFAULT_CONFIG, 228.0)

        self.assertEqual(alternative.iloc[0]["code"], "US.NVDA260618C230000")

    def test_select_option_falls_back_to_liquid_candidate_when_target_delta_is_illiquid(self) -> None:
        options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C232500",
                    "strike_price": 232.5,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 12.99,
                    "is_otm": True,
                    "otm_pct": 0.06,
                    "delta": 0.32,
                    "bid_price": 1.00,
                    "ask_price": 1.60,
                    "last_price": 1.30,
                    "volume": 2,
                    "open_interest": 15,
                },
                {
                    "code": "US.NVDA260618C245000",
                    "strike_price": 245.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 25.49,
                    "is_otm": True,
                    "otm_pct": 0.116,
                    "delta": 0.18,
                    "bid_price": 2.10,
                    "ask_price": 2.20,
                    "last_price": 2.16,
                    "volume": 120,
                    "open_interest": 450,
                },
            ]
        )

        selected = pmcc.select_option(options, pmcc.DEFAULT_CONFIG)

        self.assertEqual(selected["code"], "US.NVDA260618C245000")
        self.assertTrue(bool(selected["liquidity_ok"]))

    def test_select_option_falls_back_when_delta_is_missing(self) -> None:
        options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C232500",
                    "strike_price": 232.5,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 12.99,
                    "is_otm": True,
                    "otm_pct": 0.06,
                    "delta": pd.NA,
                    "bid_price": 4.65,
                    "ask_price": 4.75,
                    "last_price": 4.70,
                    "volume": 798,
                    "open_interest": 846,
                },
                {
                    "code": "US.NVDA260731C260000",
                    "strike_price": 260.0,
                    "strike_time": "2026-07-31",
                    "days_to_expiry": 70,
                    "distance_from_spot": 40.49,
                    "is_otm": True,
                    "otm_pct": 0.184,
                    "delta": pd.NA,
                    "bid_price": 2.00,
                    "ask_price": 2.10,
                    "last_price": 2.04,
                    "volume": 200,
                    "open_interest": 600,
                },
            ]
        )

        selected = pmcc.select_option(options, pmcc.DEFAULT_CONFIG)

        self.assertEqual(selected["code"], "US.NVDA260618C232500")
        self.assertTrue(bool(selected["liquidity_ok"]))
        self.assertTrue(pd.isna(selected["delta"]))

    def test_build_action_waits_when_candidate_structure_is_poor(self) -> None:
        option = pd.Series(
            {
                "delta": 0.55,
                "strike_price": 218.0,
                "days_to_expiry": 60,
                "otm_pct": -0.01,
                "liquidity_ok": False,
                "liquidity_reasons": ["unit test liquidity failure"],
                "bid_ask_spread_pct": 30.0,
                "last_mid_deviation_pct": 0.0,
                "volume": 0,
                "open_interest": 0,
            }
        )

        result = pmcc.build_action(
            iv_rank=60.0,
            trend="FLAT",
            stock_price=219.51,
            selected_option=option,
            config=pmcc.DEFAULT_CONFIG,
            iv_environment={"label": "FAVORABLE"},
        )

        self.assertEqual(result["action"], "WAIT")
        self.assertFalse(result["candidate_structure"]["structure_ok"])
        self.assertIn("Wait for a cleaner DTE/OTM/delta candidate", result["reason"])

    def test_event_risk_blocks_near_earnings(self) -> None:
        validation = {
            "earnings_check": {
                "days_to_earnings": 5,
                "next_earnings_date": "2026-05-27",
                "source": "unit_test",
            }
        }

        result = pmcc.build_event_risk_block("US.TEST", pd.Series({}), validation, pmcc.DEFAULT_CONFIG)

        self.assertTrue(result["blocked"])
        self.assertEqual(len(result["blocking_events"]), 1)
        self.assertEqual(result["blocking_events"][0]["type"], "earnings")

    def test_short_call_review_reports_abs_delta_and_hold_decay(self) -> None:
        short_call = pmcc.PositionInput(
            raw_code="US.NVDA260618C240000",
            underlying="US.NVDA",
            quantity=1,
            strike=240.0,
            expiry="2026-06-18",
            option_type="CALL",
            cost_price=3.74,
        )
        options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C240000",
                    "strike_price": 240.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "delta": 0.23,
                    "gamma": 0.01,
                    "vega": 0.2,
                    "theta": -0.1,
                    "implied_volatility": 39.0,
                    "bid_price": 2.95,
                    "ask_price": 3.05,
                    "last_price": 3.0,
                }
            ]
        )

        result = pmcc.analyze_short_call_position(
            short_call,
            stock_price=215.0,
            options=options,
            config=pmcc.DEFAULT_CONFIG,
            iv_rank=60.0,
            base_positions=[],
        )

        self.assertEqual(result["abs_delta"], 0.23)
        self.assertEqual(result["roll_action"], "HOLD_DECAY")
        self.assertIn("Short call is still comfortably OTM", result["reason"])

    def test_decision_engine_preserves_top_level_json_keys(self) -> None:
        enriched = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618C232500",
                    "strike_price": 232.5,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "distance_from_spot": 12.99,
                    "is_otm": True,
                    "otm_pct": 0.06,
                    "delta": 0.32,
                    "gamma": 0.01,
                    "vega": 0.2,
                    "theta": -0.1,
                    "implied_volatility": 39.0,
                    "bid_price": 4.65,
                    "ask_price": 4.75,
                    "last_price": 4.70,
                    "volume": 798,
                    "open_interest": 846,
                }
            ]
        )
        call_market_data = {
            "quote_source": "unit_test",
            "quote": pd.Series({}),
            "stock_price": 219.51,
            "enriched": enriched,
            "iv_snapshot": {"iv": 39.0, "status": "OK"},
            "data_quality": {
                "status": "OK",
                "symbol": "US.NVDA",
                "contracts_checked": 1,
                "issues": [],
                "warnings": [],
            },
        }
        config = pmcc.StrategyConfig(
            iv_rank_override=60.0,
            iv_percentile_override=60.0,
            iv_override=45.0,
            hv_override=35.0,
            trend_override="FLAT",
            enable_web_validation=False,
        )

        with patch.object(pmcc, "estimate_historical_volatility", return_value=35.0), patch.object(
            pmcc, "get_trend", return_value="FLAT"
        ):
            result = pmcc.decision_engine(
                "US.NVDA",
                config=config,
                ctx=object(),
                call_market_data=call_market_data,
            )

        expected_keys = {
            "symbol",
            "price",
            "data_quality",
            "event_block",
            "iv_environment",
            "action",
            "reason",
            "suggested_option",
            "candidate_short_call",
            "decision_support",
            "position_context",
            "roll_action",
        }
        self.assertFalse(result.keys() & {"error"})
        self.assertTrue(expected_keys.issubset(result.keys()))
        self.assertIn("candidate_structure", result["candidate_short_call"])

    def test_pmcc_two_stage_result_preserves_mixed_position_shape(self) -> None:
        base = pmcc.PositionInput(
            raw_code="US.NVDA270617C155000",
            underlying="US.NVDA",
            quantity=1,
            strike=155.0,
            expiry="2027-06-17",
            option_type="CALL",
            cost_price=64.40,
        )
        short_call = pmcc.PositionInput(
            raw_code="US.NVDA260618C240000",
            underlying="US.NVDA",
            quantity=1,
            strike=240.0,
            expiry="2026-06-18",
            option_type="CALL",
            cost_price=3.74,
        )
        short_put = pmcc.PositionInput(
            raw_code="US.NVDA260618P205000",
            underlying="US.NVDA",
            quantity=1,
            strike=205.0,
            expiry="2026-06-18",
            option_type="PUT",
            cost_price=2.90,
        )
        symbol_result = {
            "symbol": "US.NVDA",
            "price": 219.51,
            "data_quality": {"status": "OK"},
            "iv_environment": {"label": "FAVORABLE"},
            "base_positions": [{"code": base.raw_code}],
            "current_short_calls": [{"code": short_call.raw_code}],
            "current_short_puts": [{"code": short_put.raw_code}],
            "coverage": {
                "base_contracts": 1,
                "short_call_contracts": 1,
                "short_put_contracts": 1,
                "available_to_sell": 0,
            },
            "action": "WAIT",
            "reason": ["unit test"],
            "candidate_short_call": {"consider_selling": False},
            "short_call_reviews": [{"code": short_call.raw_code, "roll_action": "HOLD_DECAY"}],
            "short_put_reviews": [{"code": short_put.raw_code, "roll_action": "REVIEW"}],
            "summary_cn": "unit test summary",
            "decision_support": {"iv_environment": {"label": "FAVORABLE"}},
        }

        with patch.object(pmcc, "print_position_inventory_before_analysis"), patch.object(
            pmcc, "save_position_memory"
        ), patch.object(
            pmcc, "build_iv_rank_input_metadata", return_value={"symbols": {"US.NVDA": {"source": "unit_test"}}}
        ), patch.object(
            pmcc, "analyze_pmcc_symbol", return_value=symbol_result
        ) as analyze_mock:
            result = pmcc.build_pmcc_two_stage_result(
                {"source": "unit_test"},
                [base],
                [short_call, short_put],
                pmcc.DEFAULT_CONFIG,
            )

        analyze_mock.assert_called_once()
        analyzed_symbol, analyzed_bases, analyzed_shorts, _, analyzed_identity = analyze_mock.call_args.args
        self.assertEqual(analyzed_symbol, "US.NVDA")
        self.assertEqual([item.raw_code for item in analyzed_bases], [base.raw_code])
        self.assertEqual([item.raw_code for item in analyzed_shorts], [short_call.raw_code, short_put.raw_code])
        self.assertEqual(analyzed_identity["symbol"], "US.NVDA")
        self.assertEqual(result["mode"], "pmcc_two_stage")
        self.assertIn("iv_rank_memory", result["position_source"])
        self.assertEqual(result["recorded_positions"]["base_positions"][0]["code"], base.raw_code)
        self.assertEqual(result["recorded_positions"]["short_calls"][1]["code"], short_put.raw_code)
        self.assertEqual(result["stage_1_position_analysis"][0]["current_short_puts"][0]["code"], short_put.raw_code)
        self.assertEqual(result["stage_2_operation_recommendations"][0]["short_put_reviews"][0]["code"], short_put.raw_code)
        self.assertEqual(result["stage_2_operation_recommendations"][0]["action"], "WAIT")

    def test_pmcc_two_stage_result_groups_multiple_symbols_and_preserves_error_shape(self) -> None:
        nvda_base = pmcc.PositionInput(
            raw_code="US.NVDA270617C155000",
            underlying="US.NVDA",
            quantity=1,
            strike=155.0,
            expiry="2027-06-17",
            option_type="CALL",
            cost_price=64.40,
        )
        msft_base = pmcc.PositionInput(
            raw_code="US.MSFT270617C330000",
            underlying="US.MSFT",
            quantity=1,
            strike=330.0,
            expiry="2027-06-17",
            option_type="CALL",
            cost_price=102.01,
        )
        nvda_short = pmcc.PositionInput(
            raw_code="US.NVDA260618C240000",
            underlying="US.NVDA",
            quantity=1,
            strike=240.0,
            expiry="2026-06-18",
            option_type="CALL",
            cost_price=3.74,
        )
        msft_short = pmcc.PositionInput(
            raw_code="US.MSFT260618C445000",
            underlying="US.MSFT",
            quantity=1,
            strike=445.0,
            expiry="2026-06-18",
            option_type="CALL",
            cost_price=4.19,
        )

        def fake_analyze(symbol: str, bases, shorts, config, portfolio_identity=None):
            if symbol == "US.MSFT":
                return {
                    "symbol": symbol,
                    "price": 430.0,
                    "base_positions": [{"code": bases[0].raw_code}],
                    "current_short_calls": [{"code": shorts[0].raw_code}],
                    "coverage": {"base_contracts": 1, "short_call_contracts": 1},
                    "action": "WAIT",
                    "reason": ["unit test"],
                    "summary_cn": "msft",
                }
            return {"symbol": symbol, "error": "unit test failure"}

        with patch.object(pmcc, "print_position_inventory_before_analysis"), patch.object(
            pmcc, "save_position_memory"
        ), patch.object(
            pmcc,
            "build_iv_rank_input_metadata",
            return_value={
                "symbols": {
                    "US.MSFT": {"source": "unit_test"},
                    "US.NVDA": {"source": "unit_test"},
                }
            },
        ), patch.object(pmcc, "analyze_pmcc_symbol", side_effect=fake_analyze) as analyze_mock:
            result = pmcc.build_pmcc_two_stage_result(
                {"source": "unit_test"},
                [nvda_base, msft_base],
                [nvda_short, msft_short],
                pmcc.DEFAULT_CONFIG,
            )

        analyzed_symbols = [call.args[0] for call in analyze_mock.call_args_list]
        self.assertEqual(analyzed_symbols, ["US.MSFT", "US.NVDA"])
        self.assertEqual(len(result["stage_1_position_analysis"]), 2)
        self.assertEqual(len(result["stage_2_operation_recommendations"]), 2)
        self.assertEqual(result["stage_1_position_analysis"][0]["symbol"], "US.MSFT")
        self.assertEqual(result["stage_2_operation_recommendations"][0]["action"], "WAIT")
        self.assertEqual(result["stage_1_position_analysis"][1]["error"], "unit test failure")
        self.assertEqual(result["stage_2_operation_recommendations"][1]["error"], "unit test failure")
        self.assertEqual(result["recorded_positions"]["base_positions"][1]["code"], msft_base.raw_code)
        self.assertEqual(result["position_source"]["iv_rank_memory"]["symbols"]["US.NVDA"]["source"], "unit_test")

    def test_pmcc_multi_portfolio_result_keeps_broker_coverage_isolated(self) -> None:
        schwab_base = pmcc.PositionInput(
            raw_code="US.NVDA270617C155000",
            underlying="US.NVDA",
            quantity=1,
            strike=155.0,
            expiry="2027-06-17",
            option_type="CALL",
            cost_price=64.40,
        )
        futu_short = pmcc.PositionInput(
            raw_code="US.NVDA260618C240000",
            underlying="US.NVDA",
            quantity=1,
            strike=240.0,
            expiry="2026-06-18",
            option_type="CALL",
            cost_price=3.74,
        )

        def fake_analyze(symbol: str, bases, shorts, config, portfolio_identity=None):
            return {
                "symbol": symbol,
                "base_positions": [{"code": item.raw_code} for item in bases],
                "current_short_calls": [{"code": item.raw_code} for item in shorts],
                "coverage": {
                    "base_contracts": sum(item.quantity for item in bases),
                    "short_call_contracts": sum(item.quantity for item in shorts),
                    "available_to_sell": max(sum(item.quantity for item in bases) - sum(item.quantity for item in shorts), 0),
                },
                "action": "WAIT",
                "reason": ["unit test"],
                "summary_cn": "unit test",
            }

        with patch.object(pmcc, "print_position_inventory_before_analysis"), patch.object(
            pmcc, "save_position_memory"
        ) as save_memory_mock, patch.object(
            pmcc, "build_iv_rank_input_metadata", return_value={"symbols": {"US.NVDA": {"source": "unit_test"}}}
        ), patch.object(pmcc, "analyze_pmcc_symbol", side_effect=fake_analyze) as analyze_mock:
            result = pmcc.build_pmcc_multi_portfolio_result(
                {"source": "unit_test"},
                [
                    {
                        "portfolio_id": "FUTU",
                        "portfolio_label": "Futu OpenD",
                        "source": "futu_opend",
                        "base_positions": [],
                        "short_calls": [futu_short],
                    },
                    {
                        "portfolio_id": "SCHWAB",
                        "portfolio_label": "Schwab",
                        "source": "schwab_manual",
                        "base_positions": [schwab_base],
                        "short_calls": [],
                    },
                ],
                pmcc.DEFAULT_CONFIG,
            )

        save_memory_mock.assert_not_called()
        self.assertEqual(result["mode"], "pmcc_two_stage_multi_portfolio")
        self.assertEqual(result["position_source"]["portfolio_isolation"], "broker_account")
        self.assertEqual([call.args[1] for call in analyze_mock.call_args_list], [[], [schwab_base]])
        self.assertEqual([call.args[2] for call in analyze_mock.call_args_list], [[futu_short], []])
        self.assertEqual([call.args[4]["portfolio_id"] for call in analyze_mock.call_args_list], ["FUTU", "SCHWAB"])
        futu_stage = result["stage_1_position_analysis"][0]
        schwab_stage = result["stage_1_position_analysis"][1]
        self.assertEqual(futu_stage["portfolio_id"], "FUTU")
        self.assertEqual(futu_stage["coverage"]["base_contracts"], 0)
        self.assertEqual(futu_stage["coverage"]["short_call_contracts"], 1)
        self.assertEqual(schwab_stage["portfolio_id"], "SCHWAB")
        self.assertEqual(schwab_stage["coverage"]["base_contracts"], 1)
        self.assertEqual(schwab_stage["coverage"]["short_call_contracts"], 0)
        self.assertEqual(result["recorded_positions"]["portfolios"][0]["short_calls"][0]["code"], futu_short.raw_code)
        self.assertEqual(result["recorded_positions"]["portfolios"][1]["base_positions"][0]["code"], schwab_base.raw_code)

    def test_short_put_review_reports_review_state_and_operation_advice(self) -> None:
        short_put = pmcc.PositionInput(
            raw_code="US.NVDA260618P205000",
            underlying="US.NVDA",
            quantity=1,
            strike=205.0,
            expiry="2026-06-18",
            option_type="PUT",
            cost_price=2.90,
        )
        put_options = pd.DataFrame(
            [
                {
                    "code": "US.NVDA260618P205000",
                    "strike_price": 205.0,
                    "strike_time": "2026-06-18",
                    "days_to_expiry": 27,
                    "delta": -0.24,
                    "gamma": 0.01,
                    "vega": 0.18,
                    "theta": -0.13,
                    "implied_volatility": 40.0,
                    "bid_price": 3.60,
                    "ask_price": 3.65,
                    "last_price": 3.62,
                }
            ]
        )

        result = pmcc.analyze_short_put_position(
            short_put,
            stock_price=219.51,
            put_options=put_options,
            iv_rank=60.0,
        )

        self.assertEqual(result["roll_action"], "REVIEW")
        self.assertIn("put_delta_attention", result["rule_hits"])
        self.assertEqual(result["payoff_scenarios"]["operation_advice"], "WATCH_10PCT_DROP")
        self.assertIn("US.NVDA260618P205000", result["operation_advice_text"])

    def test_short_put_wheel_state_enters_defense_when_spot_breaches_strike(self) -> None:
        result = pmcc.build_wheel_state_for_short_put(
            stock_price=198.0,
            strike=205.0,
            dte=27,
            delta=-0.52,
            profit_capture_pct=None,
            break_even=202.1,
            mark=4.10,
            credit=2.90,
        )

        self.assertEqual(result["state"], "CSP_DEFEND_OR_ACCEPT_ASSIGNMENT")
        self.assertEqual(result["action"], "ROLL_DOWN_OUT_OR_ACCEPT_ASSIGNMENT")
        self.assertEqual(result["priority"], "HIGH")
        self.assertFalse(result["assignment_acceptable_by_breakeven"])
        self.assertTrue(any("Spot is at or below" in item for item in result["rationale"]))

    def test_short_put_payoff_scenarios_include_breakeven_and_missing_data_state(self) -> None:
        short_put = pmcc.PositionInput(
            raw_code="US.NVDA260618P205000",
            underlying="US.NVDA",
            quantity=1,
            strike=205.0,
            expiry="2026-06-18",
            option_type="PUT",
            cost_price=2.90,
        )

        result = pmcc.build_short_put_payoff_scenarios(
            short_put,
            stock_price=219.51,
            strike=205.0,
            credit=2.90,
            mark=3.62,
        )
        missing = pmcc.build_short_put_payoff_scenarios(
            short_put,
            stock_price=219.51,
            strike=None,
            credit=2.90,
            mark=3.62,
        )

        self.assertEqual(result["break_even"], 202.1)
        self.assertEqual(result["max_profit"], 290.0)
        self.assertEqual(result["current_unrealized_pnl"], -72.0)
        self.assertEqual(result["operation_advice"], "WATCH_10PCT_DROP")
        self.assertTrue(any(row["spot_at_expiry"] == 202.1 and row["pnl_total"] == 0.0 for row in result["rows"]))
        self.assertEqual(missing["operation_advice"], "MONITOR_DATA")
        self.assertEqual(missing["rows"], [])

    def test_short_put_payoff_scenarios_scale_multi_contract_assignment_loss(self) -> None:
        short_put = pmcc.PositionInput(
            raw_code="US.NVDA260618P205000",
            underlying="US.NVDA",
            quantity=2,
            strike=205.0,
            expiry="2026-06-18",
            option_type="PUT",
            cost_price=2.90,
        )

        result = pmcc.build_short_put_payoff_scenarios(
            short_put,
            stock_price=200.0,
            strike=205.0,
            credit=2.90,
            mark=8.00,
        )
        below_strike_row = next(row for row in result["rows"] if row["spot_at_expiry"] == 180.0)

        self.assertEqual(result["quantity"], 2)
        self.assertEqual(result["max_profit"], 580.0)
        self.assertEqual(result["current_unrealized_pnl"], -1020.0)
        self.assertEqual(result["operation_advice"], "DEFEND_OR_ACCEPT_ASSIGNMENT")
        self.assertTrue(below_strike_row["assigned"])
        self.assertEqual(below_strike_row["put_intrinsic_at_expiry"], 25.0)
        self.assertEqual(below_strike_row["pnl_total"], -4420.0)
        self.assertEqual(below_strike_row["assignment_cash_required"], 41000.0)
        self.assertEqual(below_strike_row["effective_share_cost"], 202.1)


if __name__ == "__main__":
    unittest.main()
