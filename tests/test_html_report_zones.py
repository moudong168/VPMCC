import sys
import types
import unittest


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

from pmcc import reports as pmcc_reports


def build_minimal_result() -> dict:
    return {
        "mode": "pmcc_two_stage_multi_portfolio",
        "position_source": {
            "source": "unit_test",
            "total_base_contracts": 1,
            "total_short_call_contracts": 1,
            "total_short_put_contracts": 0,
        },
        "recorded_positions": {
            "portfolios": [
                {
                    "portfolio_id": "FUTU",
                    "portfolio_label": "Futu OpenD",
                    "base_positions": [{"underlying": "US.NVDA", "code": "US.NVDA270617C155000", "quantity": 1}],
                    "short_calls": [{"underlying": "US.NVDA", "code": "US.NVDA260618C240000", "quantity": 1, "option_type": "CALL"}],
                },
            ],
        },
        "stage_1_position_analysis": [
            {
                "portfolio_id": "FUTU",
                "portfolio_label": "Futu OpenD",
                "symbol": "US.NVDA",
                "price": 219.51,
                "coverage": {"base_contracts": 1, "short_call_contracts": 1, "available_to_sell": 0},
                "iv_environment": {"label": "FAVORABLE"},
                "data_validation": {"status": "OK"},
            },
        ],
        "stage_2_operation_recommendations": [
            {
                "portfolio_id": "FUTU",
                "portfolio_label": "Futu OpenD",
                "symbol": "US.NVDA",
                "action": "WAIT",
                "reason": ["unit test"],
                "candidate_short_call": {"code": "US.NVDA260618C240000", "consider_selling": False},
                "short_call_reviews": [],
                "short_put_reviews": [],
                "summary_cn": "unit test summary",
            },
        ],
    }


class HtmlReportZoneTests(unittest.TestCase):
    def test_html_report_contains_zone_markers_and_nav(self) -> None:
        report = pmcc_reports.build_html_report(build_minimal_result())

        self.assertIn("zone-positions", report)
        self.assertIn('id="zone-positions"', report)
        self.assertIn("zone-ledger", report)
        self.assertIn('id="zone-ledger"', report)
        self.assertIn('id="zone-details"', report)
        self.assertIn("zone-raw", report)
        self.assertIn('id="zone-raw"', report)
        self.assertIn("report-nav", report)
        self.assertIn('href="#zone-ledger"', report)

    def test_html_report_css_contains_zone_raw_dark_zone(self) -> None:
        css = pmcc_reports.html_report_css()

        self.assertIn(".zone-raw", css)
        self.assertIn("#1f2937", css)


if __name__ == "__main__":
    unittest.main()
