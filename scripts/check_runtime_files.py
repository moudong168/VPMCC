import fnmatch
import subprocess
import sys
from pathlib import Path


PROTECTED_PATTERNS = [
    "pmcc_last_positions.json",
    "pmcc_*_positions.json",
    "pmcc_iv_history.json",
    "pmcc_iv_rank_memory.json",
    "pmcc_report.html",
    "pmcc_trade_journal.jsonl",
    "pmcc_trade_journal.csv",
    "obsidian_notes/",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.csv",
    "*.tsv",
    "*.parquet",
    "*.feather",
    "*.pkl",
    "*.pickle",
    "*.h5",
    "*.tmp",
    "*.bak",
    "*.local",
]


def load_gitignore_patterns(repo_root: Path) -> list[str]:
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns: list[str] = []
    for raw_line in gitignore.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def is_ignored_by_pattern(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/")
    if normalized_pattern.endswith("/"):
        return normalized == normalized_pattern.rstrip("/") or normalized.startswith(normalized_pattern)
    if "/" not in normalized_pattern:
        return fnmatch.fnmatch(Path(normalized).name, normalized_pattern)
    return fnmatch.fnmatch(normalized, normalized_pattern)


def missing_gitignore_patterns(repo_root: Path) -> list[str]:
    gitignore_patterns = load_gitignore_patterns(repo_root)
    return [
        pattern
        for pattern in PROTECTED_PATTERNS
        if not any(is_ignored_by_pattern(pattern.rstrip("/"), gitignore_pattern) for gitignore_pattern in gitignore_patterns)
    ]


def tracked_runtime_files(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--", *PROTECTED_PATTERNS],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    missing_patterns = missing_gitignore_patterns(repo_root)
    tracked_files = tracked_runtime_files(repo_root)

    if missing_patterns or tracked_files:
        if missing_patterns:
            print("Missing runtime patterns in .gitignore:")
            for pattern in missing_patterns:
                print(f"  - {pattern}")
        if tracked_files:
            print("Runtime files are still tracked by Git:")
            for path in tracked_files:
                print(f"  - {path}")
        return 1

    print("Runtime-file protection check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
