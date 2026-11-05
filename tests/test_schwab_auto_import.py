"""Tests for the opend-mode Schwab Position Statement auto-discovery.

Covers ``find_auto_schwab_position_csv`` / ``resolve_auto_schwab_import`` /
``remember_schwab_opend_csv`` in futu_option_decision.py:

(a) a CSV dropped in the parent directory is discovered,
(b) an already-consumed CSV (state record matches name + mtime) is not re-imported,
(c) an explicit --schwab-import-positions path wins over auto discovery,
(d) a corrupt auto CSV falls back safely without advancing the state,
plus no-CSV behaviour and the run_pmcc_opend state advance.
"""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCHWAB_SAMPLE = """Position Statement for 12345678SCHW (Individual) on 9/29/26 22:14:27
Equities and Equity Options
Instrument,Qty,Days,Trade Price,Mark,Mrk Chng,Net Liq
MSFT,,,,,,"$41,567.50"
100 17 JUN 27 480 CALL,-3,261,66.98,73.00,-3.669,"($21,900.00)"
100 17 JUN 27 330 CALL,+1,261,102.00,187.35,-5.2962,"$18,735.00"
"""

SCHWAB_SAMPLE_WITH_STOCK = SCHWAB_SAMPLE.replace('MSFT,,,,,,"$41,567.50"', 'AAPL,+200,3,180.00,181.50,+1.5,"$36,300.00"')

CORRUPT_BYTES = b"\xff\xfe\x00\x00 not a utf-8 position statement \x80\x81"


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


class SchwabAutoImportDiscoveryTests(unittest.TestCase):
    def _write_csv(self, directory: Path, name: str, content: str = SCHWAB_SAMPLE) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(content, encoding="utf-8-sig")
        return path

    def test_auto_discovers_csv_in_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            csv_path = self._write_csv(root, "2026-09-29-PositionStatement.csv")
            state_path = root / "pmcc_csv_state.json"
            decision = pmcc.resolve_auto_schwab_import(
                directories=[program_dir, root],
                state_path=state_path,
            )
            self.assertFalse(state_path.exists())  # helper never writes state by itself
        self.assertEqual(decision["mode"], "auto_import")
        self.assertEqual(decision["csv_path"], csv_path)
        self.assertEqual(sum(p.quantity for p in decision["base_positions"]), 1)
        self.assertEqual(sum(p.quantity for p in decision["short_calls"]), 3)
        self.assertEqual(decision["state_record"]["name"], csv_path.name)

    def test_already_consumed_csv_is_not_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            csv_path = self._write_csv(root, "2026-09-29-PositionStatement.csv")
            state_path = root / "pmcc_csv_state.json"
            first = pmcc.resolve_auto_schwab_import(directories=[program_dir, root], state_path=state_path)
            self.assertEqual(first["mode"], "auto_import")
            csv_mtime = csv_path.stat().st_mtime
            pmcc.remember_schwab_opend_csv(first["state_record"], state_path)
            second = pmcc.resolve_auto_schwab_import(directories=[program_dir, root], state_path=state_path)
            saved = pmcc.load_csv_state(state_path)["files"]
            self.assertEqual(saved["schwab_opend"]["name"], csv_path.name)
            self.assertEqual(saved["schwab_opend"]["mtime"], csv_mtime)
            # the csv-mode key is untouched by the opend run
            self.assertNotIn("schwab", saved)
        self.assertEqual(second["mode"], "already_consumed")
        self.assertEqual(second["csv_path"], csv_path)
        self.assertEqual(second["state_record"], None)

    def test_explicit_import_path_takes_precedence_over_auto_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            self._write_csv(root, "2026-09-29-PositionStatement.csv")
            explicit = self._write_csv(program_dir, "2026-01-02-PositionStatement.csv")
            decision = pmcc.resolve_auto_schwab_import(
                explicit_path=str(explicit),
                directories=[program_dir, root],
                state_path=root / "pmcc_csv_state.json",
            )
        self.assertEqual(decision["mode"], "explicit")
        self.assertEqual(decision["csv_path"], explicit)
        self.assertEqual(decision["base_positions"], [])
        self.assertIsNone(decision["state_record"])

    def test_corrupt_auto_csv_falls_back_without_advancing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            csv_path = root / "2026-09-29-PositionStatement.csv"
            csv_path.write_bytes(CORRUPT_BYTES)
            state_path = root / "pmcc_csv_state.json"
            decision = pmcc.resolve_auto_schwab_import(
                directories=[program_dir, root],
                state_path=state_path,
            )
            advanced = pmcc.load_csv_state(state_path)
        self.assertEqual(decision["mode"], "parse_failed")
        self.assertEqual(decision["csv_path"], csv_path)
        self.assertTrue(decision["error"])
        self.assertIsNone(decision["state_record"])
        self.assertEqual(decision["base_positions"], [])
        self.assertEqual(decision["short_calls"], [])
        self.assertNotIn("schwab_opend", advanced.get("files") or {})

    def test_empty_auto_csv_is_treated_as_parse_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            self._write_csv(root, "2026-09-29-PositionStatement.csv", content="")
            decision = pmcc.resolve_auto_schwab_import(
                directories=[program_dir, root],
                state_path=root / "pmcc_csv_state.json",
            )
        self.assertEqual(decision["mode"], "parse_failed")
        self.assertEqual(decision["error"], "no data rows")

    def _consumed_setup(self, root: Path, content: str = SCHWAB_SAMPLE_WITH_STOCK) -> Path:
        """Write a CSV and mark it consumed via a first auto-import."""
        program_dir = root / "program"
        program_dir.mkdir()
        csv_path = self._write_csv(root, "2026-09-29-PositionStatement.csv", content=content)
        state_path = root / "pmcc_csv_state.json"
        first = pmcc.resolve_auto_schwab_import(
            directories=[program_dir, root],
            state_path=state_path,
            snapshot_path=root / "missing_snapshot.json",
        )
        assert first["mode"] == "auto_import"
        pmcc.remember_schwab_opend_csv(first["state_record"], state_path)
        return csv_path

    def test_legacy_snapshot_missing_stock_key_forces_reimport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._consumed_setup(root)
            state_path = root / "pmcc_csv_state.json"
            snapshot_path = root / "pmcc_schwab_positions.json"
            snapshot_path.write_text(
                json.dumps({"source": "schwab_snapshot", "base_position_records": []}),
                encoding="utf-8",
            )
            decision = pmcc.resolve_auto_schwab_import(
                directories=[root / "program", root],
                state_path=state_path,
                snapshot_path=snapshot_path,
            )
        self.assertEqual(decision["mode"], "auto_import")
        self.assertEqual(decision["csv_path"], csv_path)
        self.assertEqual(sum(p.quantity for p in decision["stock_positions"]), 200)
        self.assertEqual(decision["state_record"]["name"], csv_path.name)

    def test_snapshot_with_stock_key_keeps_already_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._consumed_setup(root)
            state_path = root / "pmcc_csv_state.json"
            snapshot_path = root / "pmcc_schwab_positions.json"
            snapshot_path.write_text(
                json.dumps({"source": "schwab_snapshot", "stock_position_records": []}),
                encoding="utf-8",
            )
            decision = pmcc.resolve_auto_schwab_import(
                directories=[root / "program", root],
                state_path=state_path,
                snapshot_path=snapshot_path,
            )
        self.assertEqual(decision["mode"], "already_consumed")
        self.assertEqual(decision["csv_path"], csv_path)
        self.assertIsNone(decision["state_record"])

    def test_missing_snapshot_keeps_already_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._consumed_setup(root)
            state_path = root / "pmcc_csv_state.json"
            decision = pmcc.resolve_auto_schwab_import(
                directories=[root / "program", root],
                state_path=state_path,
                snapshot_path=root / "no_such_snapshot.json",
            )
        self.assertEqual(decision["mode"], "already_consumed")
        self.assertEqual(decision["csv_path"], csv_path)
        self.assertIsNone(decision["state_record"])

    def test_newest_csv_across_program_and_parent_directory_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            self._write_csv(program_dir, "2026-09-28-PositionStatement.csv")
            newest = self._write_csv(root, "2026-09-29-PositionStatement.csv")
            state_path = root / "pmcc_csv_state.json"
            decision = pmcc.resolve_auto_schwab_import(
                directories=[program_dir, root],
                state_path=state_path,
            )
            reversed_order = pmcc.resolve_auto_schwab_import(
                directories=[root, program_dir],
                state_path=state_path,
            )
        self.assertEqual(decision["csv_path"], newest)
        self.assertEqual(reversed_order["csv_path"], newest)

    def test_no_csv_anywhere_returns_none_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            (root / "notes.txt").write_text("no position export here", encoding="utf-8")
            decision = pmcc.resolve_auto_schwab_import(
                directories=[program_dir, root],
                state_path=root / "pmcc_csv_state.json",
            )
            state_path = root / "pmcc_csv_state.json"
            self.assertFalse(state_path.exists())
        self.assertEqual(decision["mode"], "none")
        self.assertIsNone(decision["csv_path"])
        self.assertIsNone(decision["state_record"])


class RunPmccOpendAutoImportTests(unittest.TestCase):
    """run_pmcc_opend consumes the discovered CSV and advances the state afterwards."""

    def test_run_pmcc_opend_auto_import_prints_line_and_advances_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            csv_path = root / "2026-09-29-PositionStatement.csv"
            csv_path.write_text(SCHWAB_SAMPLE, encoding="utf-8-sig")
            state_path = root / "pmcc_csv_state.json"
            positions_file = root / "pmcc_schwab_positions.json"
            stdout = io.StringIO()
            with patch.object(pmcc, "load_broker_positions", return_value=([], [], [], {"source": "stub"})), patch.object(
                pmcc, "save_broker_positions"
            ) as save_positions_mock, patch.object(
                pmcc, "print_broker_position_confirmation"
            ), patch.object(
                pmcc, "build_pmcc_multi_portfolio_result", return_value={"ok": True}
            ), patch.object(
                pmcc, "CSV_STATE_FILE", state_path
            ), patch.object(
                pmcc, "SCHWAB_POSITIONS_FILE", positions_file
            ), contextlib.redirect_stdout(stdout):
                result = pmcc.run_pmcc_opend(
                    pmcc.DEFAULT_CONFIG,
                    prompt_external=False,
                    use_cboe=True,
                    auto_schwab_dirs=[program_dir, root],
                )
            output = stdout.getvalue()
            state = pmcc.load_csv_state(state_path)
            saved_quantities = [
                (sum(p.quantity for p in call.args[2]), sum(p.quantity for p in call.args[3]))
                for call in save_positions_mock.call_args_list
            ]
            self.assertFalse(positions_file.exists())
        self.assertEqual(result, {"ok": True})
        self.assertIn(f"[Schwab CSV] auto-imported {csv_path.name} -> 1 base / 3 short.", output)
        self.assertIn((1, 3), saved_quantities)
        self.assertEqual(state["files"]["schwab_opend"]["name"], csv_path.name)

    def test_run_pmcc_opend_corrupt_csv_falls_back_and_keeps_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program_dir = root / "program"
            program_dir.mkdir()
            csv_path = root / "2026-09-29-PositionStatement.csv"
            csv_path.write_bytes(CORRUPT_BYTES)
            state_path = root / "pmcc_csv_state.json"
            stdout = io.StringIO()
            with patch.object(pmcc, "load_broker_positions", return_value=([], [], [], {"source": "stub"})), patch.object(
                pmcc, "save_broker_positions"
            ), patch.object(
                pmcc, "print_broker_position_confirmation"
            ), patch.object(
                pmcc, "build_pmcc_multi_portfolio_result", return_value={"ok": True}
            ), patch.object(
                pmcc, "CSV_STATE_FILE", state_path
            ), contextlib.redirect_stdout(stdout):
                result = pmcc.run_pmcc_opend(
                    pmcc.DEFAULT_CONFIG,
                    prompt_external=False,
                    use_cboe=True,
                    auto_schwab_dirs=[program_dir, root],
                )
            output = stdout.getvalue()
            state = pmcc.load_csv_state(state_path)
        self.assertEqual(result, {"ok": True})
        self.assertIn(f"[Schwab CSV] WARNING: failed to parse {csv_path.name}", output)
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
