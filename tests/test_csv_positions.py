"""Tests for pmcc.csv_positions: CSV discovery, Schwab/Futu parsing, state tracking."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from pmcc.csv_positions import (
    find_latest_position_csvs,
    is_new_csv,
    load_csv_state,
    parse_futu_position_csv,
    parse_schwab_position_statement,
    save_csv_state,
)

SCHWAB_SAMPLE = """Position Statement for 12345678SCHW (Individual) on 9/29/26 22:14:27
Equities and Equity Options
Instrument,Qty,Days,Trade Price,Mark,Mrk Chng,Net Liq
DRAM,+200,,69.3705,61.11,+1.41,"$12,222.00"
GOOGL,,,,,,"$6,761.50"
100 16 OCT 26 365 CALL,-1,17,2.05,1.935,-.6663,($193.50)
100 21 JAN 28 330 CALL,+1,479,100.80,69.55,-2.5556,"$6,955.00"
MSFT,,,,,,"$41,567.50"
100 17 JUN 27 480 CALL,-3,261,66.98,73.00,-3.669,"($21,900.00)"
100 17 JUN 27 330 CALL,+1,261,102.00,187.35,-5.2962,"$18,735.00"
"""

FUTU_SAMPLE = """"代码","名称","持有数量","现价","平均成本价","市值"
"DRAM","Roundhill Memory ETF","200","61.030","67.907","12,206.00"
"KO","可口可乐","0","87.135","79.50","0.00"
"MSFT270617C390/480","MSFT 垂直策略","1","62.575","2.65","6,257.50"
"MSFT270617C390000","MSFT 270617 390.00C","1","135.58","69.50","13,557.50"
"MSFT270617C480000","MSFT 270617 480.00C","-1","73.00","66.85","-7,300.00"
"NVDA261002P215000","NVDA 261002 215.00P","-1","0.31","1.02","-31.00"
"NVDA260118C240000","NVDA 260118 240.00C","0","5.00","6.00","0.00"
"""


class CsvPositionsTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8-sig")
        return path

    def test_parse_schwab_statement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "2026-09-29-PositionStatement.csv", SCHWAB_SAMPLE)
            base, shorts, meta = parse_schwab_position_statement(path)
        self.assertEqual(meta["rows_seen"], 10)
        # base: GOOGL 330C x1, MSFT 330C x1 ; shorts: GOOGL 365C x1, MSFT 480C x3
        self.assertEqual(sum(p.quantity for p in base), 2)
        self.assertEqual(sum(p.quantity for p in shorts), 4)
        codes = {p.raw_code for p in base} | {p.raw_code for p in shorts}
        self.assertIn("US.GOOGL261016C365000", codes)
        self.assertIn("US.MSFT270617C480000", codes)
        msft480 = next(p for p in shorts if p.strike == 480.0)
        self.assertEqual(msft480.quantity, 3)
        self.assertAlmostEqual(msft480.cost_price, 66.98)

    def test_parse_futu_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "持仓-保证金综合账户(XXXX)-20260929-221417.csv", FUTU_SAMPLE)
            base, shorts, meta = parse_futu_position_csv(path)
        # stocks skipped, strategy row skipped, zero-qty skipped
        self.assertEqual(len(base), 1)
        self.assertEqual(len(shorts), 2)
        self.assertEqual(base[0].raw_code, "US.MSFT270617C390000")
        self.assertAlmostEqual(base[0].cost_price, 69.50)
        short_codes = {p.raw_code: p.quantity for p in shorts}
        self.assertEqual(short_codes["US.MSFT270617C480000"], 1)
        self.assertEqual(short_codes["US.NVDA261002P215000"], 1)
        # put leg keeps PUT type
        nvda_put = next(p for p in shorts if "P215000" in p.raw_code)
        self.assertEqual(nvda_put.option_type, "PUT")
        skipped_reasons = {s["reason"] for s in meta["skipped"]}
        self.assertIn("strategy_summary_row", skipped_reasons)
        self.assertIn("zero_or_missing_quantity", skipped_reasons)

    def test_find_latest_csvs_orders_by_filename_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "2026-09-28-PositionStatement.csv", SCHWAB_SAMPLE)
            newest_schwab = self._write(root, "2026-09-29-PositionStatement.csv", SCHWAB_SAMPLE)
            self._write(root, "持仓-保证金综合账户(XXXX)-20260928-101010.csv", FUTU_SAMPLE)
            newest_futu = self._write(root, "持仓-保证金综合账户(XXXX)-20260929-221417.csv", FUTU_SAMPLE)
            found = find_latest_position_csvs(root)
        self.assertEqual(found["schwab"], newest_schwab)
        self.assertEqual(found["futu"], newest_futu)

    def test_find_latest_csvs_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = find_latest_position_csvs(Path(tmp))
        self.assertIsNone(found["schwab"])
        self.assertIsNone(found["futu"])
        self.assertIsNone(find_latest_position_csvs(Path(tmp) / "nope")["schwab"])

    def test_csv_state_roundtrip_and_is_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "pmcc_csv_state.json"
            self.assertEqual(load_csv_state(state_path), {})
            csv_path = self._write(root, "2026-09-29-PositionStatement.csv", SCHWAB_SAMPLE)
            # never consumed -> new
            self.assertTrue(is_new_csv(csv_path, None))
            save_csv_state(state_path, {"schwab": {"name": csv_path.name, "mtime": csv_path.stat().st_mtime}})
            state = load_csv_state(state_path)
            self.assertFalse(is_new_csv(csv_path, state["files"]["schwab"]))
            # same name re-exported (mtime bumped) -> new again
            os.utime(csv_path, (csv_path.stat().st_atime + 5, csv_path.stat().st_mtime + 5))
            self.assertTrue(is_new_csv(csv_path, state["files"]["schwab"]))
            # corrupt state file -> treated as missing
            state_path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_csv_state(state_path), {})


if __name__ == "__main__":
    unittest.main()


class CboeIvNormalizationTests(unittest.TestCase):
    def test_cboe_iv_decimal_normalized_to_percent(self) -> None:
        import io
        import json
        from unittest import mock

        from pmcc.data_cboe import fetch_cboe_data

        payload = {
            "data": {
                "symbol": "NVDA",
                "current_price": 230.04,
                "options": [
                    {
                        "option": "NVDA261030C00235000",
                        "iv": 0.3052,
                        "delta": 0.4255,
                        "bid": 5.3,
                        "ask": 5.45,
                        "open_interest": 120,
                        "volume": 35,
                    }
                ],
            }
        }
        raw = json.dumps(payload).encode("utf-8")

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return raw

        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            chain, meta = fetch_cboe_data("US.NVDA")

        self.assertEqual(len(chain), 1)
        # 0.3052 (decimal) -> 30.52 (percent), matching Futu-chain convention
        self.assertAlmostEqual(float(chain.iloc[0]["implied_volatility"]), 30.52, places=2)
        self.assertEqual(meta["current_price"], 230.04)
