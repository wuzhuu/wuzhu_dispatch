from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, print_rows, resolve_db_path, write_maintenance_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily local maintenance and persist maintenance_log.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--with-backtest", action="store_true")
    parser.add_argument("--with-visuals", action="store_true", default=True)
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    tasks = [
        ["scripts/inspect_database_state.py", "--db-path", str(db_path)],
        ["scripts/validate_price_data.py", "--db-path", str(db_path)],
        ["scripts/run_analysis_pipeline.py", "--db-path", str(db_path)],
        ["scripts/validate_analysis_outputs.py", "--db-path", str(db_path)],
    ]
    if args.with_backtest:
        tasks.extend(
            [
                ["scripts/backtest_factor.py", "--db-path", str(db_path), "--start", "2025-01-01", "--top-n", "20"],
                ["scripts/validate_backtest_outputs.py", "--db-path", str(db_path)],
            ]
        )
    if args.with_visuals:
        tasks.extend(
            [
                ["scripts/build_visual_dashboard.py", "--db-path", str(db_path)],
                ["scripts/validate_visual_outputs.py", "--db-path", str(db_path)],
            ]
        )
    rows = []
    for task in tasks:
        result = subprocess.run([sys.executable, *task], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
        rows.append(check_row("daily_maintenance_" + Path(task[0]).stem, "OK" if result.returncode == 0 else "FAIL", (result.stdout or result.stderr).strip()[-800:], result.returncode, 0))
    print_rows(rows)
    write_maintenance_rows(db_path, "maintenance_log", rows)
    print("saved: lake/maintenance_log")


if __name__ == "__main__":
    main()
