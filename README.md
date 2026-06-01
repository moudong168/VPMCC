# VPMCC

VPMCC is a local PMCC (Poor Man's Covered Call) decision helper for US options.
It reads option data and positions, evaluates PMCC coverage and roll candidates,
and produces text/HTML reports for review before trading.

The project is designed for personal research and decision support. It is not
financial advice, and it does not place trades.

## Features

- Analyze PMCC long call and short call structures.
- Read live quote and position data through Futu OpenD.
- Import Schwab / thinkorswim position statement CSV or TXT exports.
- Keep Futu and Schwab positions isolated when evaluating coverage and P/L.
- Track local trade-journal events in an append-only JSONL file outside Git.
- Render plain-text and HTML reports.
- Guard against committing local runtime data with `.gitignore` and
  `scripts/check_runtime_files.py`.

## Requirements

- Python 3.10 or newer is recommended.
- Futu OpenD is required for live Futu quote / position workflows.
- Python dependencies:

```powershell
pip install -r requirements.txt
```

## Quick Start

Run a manual single-symbol sample without web validation:

```powershell
python futu_option_decision.py US.NVDA --no-web-validation --iv-rank 60 --iv-percentile 60 --iv 45 --hv 35 --trend FLAT
```

Run the Futu OpenD workflow:

```powershell
.\run_pmcc_opend.ps1
```

Before using the OpenD workflow, start Futu OpenD, log in, and keep it running.
By default the program connects to `127.0.0.1:11111`.

## Common Commands

Import Schwab / thinkorswim positions:

```powershell
python futu_option_decision.py --pmcc-opend --schwab-import-positions "path\to\positions.csv"
```

Add manual non-Futu positions:

```powershell
python futu_option_decision.py --pmcc-opend --external-base "US.NVDA260618C120000,1,35.20" --external-short "US.NVDA260702C150000,1,5.10"
```

Record a reviewed trade-journal event:

```powershell
python futu_option_decision.py --trade-journal-event "event_date=2026-05-26; broker=SCHWAB; symbol=US.NVDA; strategy=PMCC; action=OPEN_SHORT_CALL; quantity=1; price=5.10; confirm=yes"
```

## Private Runtime Files

The program may create local files such as:

- `pmcc_last_positions.json`
- `pmcc_futu_positions.json`
- `pmcc_schwab_positions.json`
- `pmcc_iv_history.json`
- `pmcc_iv_rank_memory.json`
- `pmcc_report.html`
- `.runtime-appdata/`
- `pmcc_trade_journal.jsonl`

These files can contain local portfolio snapshots, report output, logs, or
trade-journal data. They are intentionally ignored by Git and should not be
committed to a public repository.

Check runtime-file protection with:

```powershell
python scripts\check_runtime_files.py
```

## Tests

Run the test suite:

```powershell
python -m unittest discover -s tests -v
```

Compile-check the main script:

```powershell
python -m py_compile futu_option_decision.py
```

## Notes

VPMCC is a review tool. Always verify option quotes, liquidity, assignment risk,
earnings dates, broker positions, and order details directly with your broker
before making any trade decision.
