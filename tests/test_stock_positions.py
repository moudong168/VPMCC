"""Tests for stock positions (third position class, option_type="STOCK").

Covers: Schwab CSV parsing, Futu CSV parsing, position_from_record round-trip,
coverage computation with stock cover contracts, and HTML position confirmation.
"""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCHWAB_SAMPLE = """Position Statement for 12345678SCHW (Individual) on 9/29/26 22:14:27
Equities and Equity Options
Instrument,Qty,Days,Trade Price,Mark,Mrk Chng,Net Liq
DRAM,+200,,69.3705,61.11,+1.41,"$12,222.00"
GOOGL,,,,,,"$6,761.50"
100 16 OCT 26 365 CALL,-1,17,2.05,1.935,-.6663,($193.50)
100 21 JAN 28 330 CALL,+1,479,100.80,69.55,-2.5556,"$6,955.00"
"""

FUTU_SAMPLE = """"代码","名称","持有数量","现价","平均成本价","市值"
"DRAM","Roundhill Memory ETF","200","61.030","67.907","12,206.00"
"MSFT270617C390000","MSFT 270617 390.00C","1","135.58","69.50","13,557.50"
"MSFT270617C480000","MSFT 270617 480.00C","-1","73.00","66.85","-7,300.00"
"""


def install_futu_stub() -> None:
    futu_stub = types.ModuleType("futu")
    futu_stub.OpenQuoteContext = type("OpenQuoteContext", (), {})
    futu_stub.OptionType = type("OptionType", (), {"CALL": "CALL", "PUT": "PUT"})
    futu_stub.RET_OK = 0
    futu_stub.TrdEnv = type("TrdEnv", (), {"REAL": "REAL"})
    sys.modules["futu"] = futu_stub


try:
    import futu_option_decision as pmcc
except ModuleNotFoundError as exc:  # pragma: no cover - depends on futu being installed
    if exc.name != "futu":
        raise
    install_futu_stub()
    import futu_option_decision as pmcc

from pmcc.csv_positions import parse_futu_position_csv, parse_schwab_position_statement
from pmcc.positions import position_from_record, position_to_record
from pmcc.reports import html_recorded_positions_section


class StockPositionParsingTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8-sig")
        return path

    def test_schwab_statement_stock_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "2026-09-29-PositionStatement.csv", SCHWAB_SAMPLE)
            parsed = parse_schwab_position_statement(path)
        self.assertEqual(len(parsed), 4)
        base, shorts, stocks, meta = parsed
        # option legs unchanged
        self.assertEqual(sum(p.quantity for p in base), 1)
        self.assertEqual(sum(p.quantity for p in shorts), 1)
        self.assertEqual(len(stocks), 1)
        stock = stocks[0]
        self.assertEqual(stock.raw_code, "US.DRAM")
        self.assertEqual(stock.underlying, "US.DRAM")
        self.assertEqual(stock.quantity, 200)
        self.assertEqual(stock.option_type, "STOCK")
        self.assertIsNone(stock.strike)
        self.assertIsNone(stock.expiry)
        self.assertAlmostEqual(stock.cost_price, 69.3705)
        self.assertEqual(meta["stock_shares"], 200)

    def test_futu_csv_stock_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "持仓-保证金综合账户(XXXX)-20260929-221417.csv", FUTU_SAMPLE)
            parsed = parse_futu_position_csv(path)
        self.assertEqual(len(parsed), 4)
        base, shorts, stocks, meta = parsed
        self.assertEqual(sum(p.quantity for p in base), 1)
        self.assertEqual(sum(p.quantity for p in shorts), 1)
        self.assertEqual(len(stocks), 1)
        stock = stocks[0]
        self.assertEqual(stock.raw_code, "US.DRAM")
        self.assertEqual(stock.underlying, "US.DRAM")
        self.assertEqual(stock.quantity, 200)
        self.assertEqual(stock.option_type, "STOCK")
        self.assertIsNone(stock.strike)
        self.assertIsNone(stock.expiry)
        self.assertAlmostEqual(stock.cost_price, 67.907)
        self.assertEqual(meta["stock_shares"], 200)

    def test_position_from_record_stock_roundtrip(self) -> None:
        stock = pmcc.PositionInput(
            raw_code="US.DRAM",
            underlying="US.DRAM",
            quantity=200,
            strike=None,
            expiry=None,
            option_type="STOCK",
            cost_price=69.3705,
        )
        record = position_to_record(stock)
        restored = position_from_record(record)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.raw_code, "US.DRAM")
        self.assertEqual(restored.underlying, "US.DRAM")
        self.assertEqual(restored.quantity, 200)
        self.assertEqual(restored.option_type, "STOCK")
        self.assertIsNone(restored.strike)
        self.assertIsNone(restored.expiry)
        self.assertAlmostEqual(restored.cost_price, 69.3705)


class StockCoverageTests(unittest.TestCase):
    def test_coverage_counts_stock_cover_contracts(self) -> None:
        """analyze_pmcc_symbol with no market data still emits coverage with stock fields.

        Market data is stubbed to raise so the run takes the error path; the
        coverage block is computed unconditionally afterwards, so we verify
        stock_shares / stock_cover_contracts / available_to_sell there.
        """
        stock = pmcc.PositionInput(
            raw_code="US.DRAM",
            underlying="US.DRAM",
            quantity=200,
            strike=None,
            expiry=None,
            option_type="STOCK",
            cost_price=69.3705,
        )
        with patch.object(pmcc, "load_cboe_market_data", side_effect=RuntimeError("no network in unit test")):
            result = pmcc.analyze_pmcc_symbol(
                "US.DRAM",
                [],
                [],
                pmcc.DEFAULT_CONFIG,
                use_cboe=True,
                stock_positions=[stock],
            )
        self.assertIn("error", result)
        coverage = result["coverage"]
        self.assertEqual(coverage["base_contracts"], 0)
        self.assertEqual(coverage["stock_shares"], 200)
        self.assertEqual(coverage["stock_cover_contracts"], 2)
        self.assertEqual(coverage["available_to_sell"], 2)
        self.assertEqual(coverage["max_new_short_calls"], 2)
        self.assertEqual(result["stock_positions"][0]["code"], "US.DRAM")
        self.assertEqual(result["stock_positions"][0]["quantity"], 200)


class StockPositionReportTests(unittest.TestCase):
    STOCK_RECORD = {
        "code": "US.DRAM",
        "underlying": "US.DRAM",
        "quantity": 200,
        "strike": None,
        "expiry": None,
        "option_type": "STOCK",
        "cost_price": 69.3705,
    }

    def test_single_platform_report_shows_stock_section(self) -> None:
        html = html_recorded_positions_section({"stock_positions": [dict(self.STOCK_RECORD)]})
        self.assertIn("正股持仓", html)
        self.assertIn("US.DRAM", html)
        self.assertIn("200", html)

    def test_multi_platform_report_shows_stock_section(self) -> None:
        recorded = {
            "portfolios": [
                {
                    "portfolio_id": "SCHWAB",
                    "portfolio_label": "Schwab",
                    "base_positions": [],
                    "short_calls": [],
                    "stock_positions": [dict(self.STOCK_RECORD)],
                }
            ]
        }
        html = html_recorded_positions_section(recorded)
        self.assertIn("正股持仓", html)
        self.assertIn("US.DRAM", html)


if __name__ == "__main__":
    unittest.main()
