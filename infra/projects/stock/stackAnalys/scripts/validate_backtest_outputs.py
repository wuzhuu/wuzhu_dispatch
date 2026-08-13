from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, dataset_relation, print_rows, resolve_data_root, resolve_db_path, write_maintenance_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate persisted backtest outputs.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    data_root = resolve_data_root(db_path)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = validate_backtest(conn, data_root)
    finally:
        conn.close()
    print_rows(rows)
    if not args.no_persist:
        write_maintenance_rows(db_path, "backtest_validation", rows)
        print("saved: lake/backtest_validation")


def validate_backtest(conn: duckdb.DuckDBPyConnection, data_root: Path) -> list[dict]:
    rows: list[dict] = []
    result_rel = dataset_relation(conn, data_root, "backtest_result")
    monthly_rel = dataset_relation(conn, data_root, "backtest_monthly_returns")
    holdings_rel = dataset_relation(conn, data_root, "backtest_holdings")
    rows.append(check_row("backtest_result_exists", "OK" if result_rel else "FAIL", str(result_rel), 1 if result_rel else 0, 1))
    rows.append(check_row("backtest_monthly_returns_exists", "OK" if monthly_rel else "FAIL", str(monthly_rel), 1 if monthly_rel else 0, 1))
    rows.append(check_row("backtest_holdings_exists", "OK" if holdings_rel else "FAIL", str(holdings_rel), 1 if holdings_rel else 0, 1))
    if not result_rel:
        return rows
    result = conn.execute(f"SELECT * FROM {result_rel} ORDER BY created_at DESC LIMIT 1").df()
    if result.empty:
        rows.append(check_row("backtest_result_rows", "FAIL", "no rows", 0, 1))
        return rows
    row = result.iloc[0]
    monthly = pd.DataFrame()
    if monthly_rel:
        monthly = conn.execute(f"SELECT * FROM {monthly_rel} WHERE run_id = ? ORDER BY rebalance_date", [row["run_id"]]).df()
    if monthly.empty and pd.notna(row.get("summary_json")):
        payload = json.loads(row["summary_json"])
        monthly = pd.DataFrame(payload.get("monthly_returns", []))
    rows.append(check_row("backtest_month_count", "OK" if len(monthly) else "FAIL", f"months={len(monthly)}", len(monthly), 1))
    if not monthly.empty:
        equity = pd.to_numeric(monthly["equity"], errors="coerce")
        returns = pd.to_numeric(monthly["return"], errors="coerce")
        drawdown = equity / equity.cummax() - 1
        checks = {
            "total_return_matches_equity": (float(equity.iloc[-1] - 1), row.get("total_return")),
            "max_drawdown_matches_curve": (float(drawdown.min()), row.get("max_drawdown")),
            "best_month_matches": (float(returns.max()), _summary_value(row, "best_month")),
            "worst_month_matches": (float(returns.min()), _summary_value(row, "worst_month")),
        }
        for name, (actual, expected) in checks.items():
            ok = expected is not None and pd.notna(expected) and abs(float(actual) - float(expected)) < 1e-9
            rows.append(check_row(name, "OK" if ok else "FAIL", f"actual={actual} expected={expected}", actual, expected))
    if holdings_rel:
        h_count = conn.execute(f"SELECT COUNT(*) FROM {holdings_rel} WHERE run_id = ?", [row["run_id"]]).fetchone()[0]
        rows.append(check_row("backtest_holdings_rows", "OK" if h_count else "FAIL", f"rows={h_count}", h_count, 1))
    return rows


def _summary_value(row: pd.Series, key: str):
    if key in row and pd.notna(row[key]):
        return row[key]
    if pd.notna(row.get("summary_json")):
        payload = json.loads(row["summary_json"])
        return payload.get("summary", {}).get(key)
    return None


if __name__ == "__main__":
    main()
