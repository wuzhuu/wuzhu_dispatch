from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, print_rows, resolve_db_path, write_maintenance_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Database maintenance repair controller. Defaults to dry-run.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", help="Execute selected non-destructive repairs.")
    parser.add_argument("--fix-views", action="store_true")
    parser.add_argument("--fix-analysis", action="store_true")
    parser.add_argument("--fix-backtest", action="store_true")
    parser.add_argument("--fix-visualization", action="store_true")
    parser.add_argument("--fix-dedup-view", action="store_true")
    parser.add_argument("--clean-engine-outputs", action="store_true", help="Report legacy report outputs; does not delete without code changes.")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    rows: list[dict] = []
    try:
        rows.append(check_row("repair_mode", "OK", "execute" if args.execute else "dry-run", int(args.execute), 1))
        if args.fix_views:
            rows.extend(_run_or_report(args.execute, "fix_views", ["scripts/repair_database_views.py", "--db-path", str(db_path)]))
        if args.fix_dedup_view:
            cmd = ["scripts/repair_price_duplicates.py", "--db-path", str(db_path), "--create-dedup-view"]
            if args.execute:
                cmd.append("--execute")
            rows.extend(_run_or_report(args.execute, "fix_dedup_view", cmd))
        if args.fix_analysis:
            rows.extend(_run_or_report(args.execute, "fix_analysis", ["scripts/run_analysis_pipeline.py", "--db-path", str(db_path)]))
        if args.fix_backtest:
            rows.extend(_run_or_report(args.execute, "fix_backtest", ["scripts/backtest_factor.py", "--db-path", str(db_path), "--start", "2025-01-01", "--top-n", "20"]))
        if args.fix_visualization:
            rows.extend(_run_or_report(args.execute, "fix_visualization", ["scripts/build_visual_dashboard.py", "--db-path", str(db_path)]))
        if args.clean_engine_outputs:
            rows.append(check_row("clean_engine_outputs", "DRY_RUN", "legacy outputs are reported only; no files deleted", 0, 0))
        if not any([args.fix_views, args.fix_analysis, args.fix_backtest, args.fix_visualization, args.fix_dedup_view, args.clean_engine_outputs]):
            rows.append(check_row("repair_noop", "DRY_RUN", "no fix flag selected", 0, 0))
        print_rows(rows)
        write_maintenance_rows(db_path, "database_repair_log", rows)
        print("saved: lake/database_repair_log")
    finally:
        pass


def _run_or_report(execute: bool, name: str, cmd: list[str]) -> list[dict]:
    if not execute:
        return [check_row(name, "DRY_RUN", "would run: " + " ".join([sys.executable, *cmd]), 0, 1)]
    result = subprocess.run([sys.executable, *cmd], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    status = "OK" if result.returncode == 0 else "FAIL"
    message = (result.stdout or result.stderr).strip()[-800:]
    return [check_row(name, status, message, result.returncode, 0)]


if __name__ == "__main__":
    main()
