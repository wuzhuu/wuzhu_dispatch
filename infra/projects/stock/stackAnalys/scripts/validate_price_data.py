from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, columns, dataset_relation, print_rows, resolve_data_root, resolve_db_path, write_maintenance_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate daily_price quality without modifying raw data.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    data_root = resolve_data_root(db_path)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        relation = dataset_relation(conn, data_root, "daily_price")
        rows = [check_row("daily_price_exists", "FAIL", "daily_price is missing", 0, 1)] if not relation else validate_price(conn, relation)
    finally:
        conn.close()
    print_rows(rows)
    if not args.no_persist:
        write_maintenance_rows(db_path, "data_quality_price", rows)
        print("saved: lake/data_quality_price")


def validate_price(conn: duckdb.DuckDBPyConnection, relation: str) -> list[dict]:
    cols = columns(conn, relation)
    rows = [check_row("daily_price_exists", "OK", "daily_price is readable", 1, 1)]
    stats = conn.execute(f"SELECT COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date) FROM {relation}").fetchone()
    rows.extend(
        [
            check_row("daily_price_row_count", "OK" if stats[0] else "FAIL", f"rows={stats[0]}", stats[0], 1),
            check_row("daily_price_stock_count", "OK" if stats[1] else "FAIL", f"stocks={stats[1]}", stats[1], 1),
            check_row("daily_price_date_range", "OK" if stats[2] and stats[3] else "FAIL", f"{stats[2]}..{stats[3]}", None, None),
        ]
    )
    if {"ts_code", "trade_date", "adjust_type"}.issubset(cols):
        dup = conn.execute(
            f"""
            SELECT COUNT(*) duplicate_keys, COALESCE(SUM(c - 1), 0) duplicate_rows
            FROM (
                SELECT ts_code, trade_date, adjust_type, COUNT(*) AS c
                FROM {relation}
                GROUP BY 1, 2, 3
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        rows.append(check_row("daily_price_duplicate_keys", "OK" if dup[0] == 0 else "FAIL", f"duplicate_keys={dup[0]}", dup[0], 0))
        rows.append(check_row("daily_price_duplicate_rows", "OK" if dup[1] == 0 else "FAIL", f"duplicate_rows={dup[1]}", dup[1], 0))
    for check_name, predicate in {
        "non_positive_close": "close <= 0",
        "high_lower_than_low": "high < low",
        "close_outside_high_low": "close > high OR close < low",
    }.items():
        if all(col in cols for col in ("close", "high", "low")):
            count = conn.execute(f"SELECT COUNT(*) FROM {relation} WHERE {predicate}").fetchone()[0]
            rows.append(check_row(check_name, "OK" if count == 0 else "FAIL", f"rows={count}", count, 0))
    if {"ts_code", "trade_date", "close"}.issubset(cols):
        abnormal = conn.execute(
            f"""
            WITH x AS (
                SELECT ts_code, trade_date, close,
                       close / NULLIF(LAG(close) OVER (PARTITION BY ts_code ORDER BY trade_date), 0) - 1 AS ret
                FROM {relation}
            )
            SELECT COUNT(*) FROM x WHERE ABS(ret) > 0.5
            """
        ).fetchone()[0]
        rows.append(check_row("abnormal_abs_return_gt_50pct", "WARN" if abnormal else "OK", f"rows={abnormal}", abnormal, 0))
    return rows


if __name__ == "__main__":
    main()
