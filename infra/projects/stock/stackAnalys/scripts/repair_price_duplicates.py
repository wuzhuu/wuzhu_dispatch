from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, dataset_relation, print_rows, resolve_data_root, resolve_db_path, write_maintenance_rows
from src.storage.lake_store import LakeStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Report or repair daily_price duplicate keys. Defaults to dry-run.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--create-dedup-view", action="store_true", help="Create v_daily_price_dedup view only.")
    parser.add_argument("--create-clean-dataset", action="store_true", help="Create clean_daily_price parquet dataset without deleting raw data.")
    parser.add_argument("--dedupe-partitions", action="store_true", help="Rewrite daily_price parquet partitions in place after backing them up.")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", help="Allow selected repairs. Partition rewrites back up old parquet first.")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    data_root = resolve_data_root(db_path)
    conn = duckdb.connect(str(db_path))
    rows: list[dict] = []
    try:
        relation = dataset_relation(conn, data_root, "daily_price")
        if not relation:
            rows.append(check_row("daily_price_exists", "FAIL", "daily_price is missing", 0, 1))
        else:
            rows.extend(_duplicate_report(conn, relation))
            if args.execute and args.create_dedup_view:
                conn.execute(f"CREATE OR REPLACE VIEW v_daily_price_dedup AS {_dedup_sql(relation)}")
                rows.append(check_row("create_dedup_view", "OK", "created v_daily_price_dedup", 1, 1))
            elif args.create_dedup_view:
                rows.append(check_row("create_dedup_view", "DRY_RUN", "would create v_daily_price_dedup", 0, 1))
            if args.execute and args.create_clean_dataset:
                out_root = data_root / "lake" / "clean_daily_price"
                out_root.mkdir(parents=True, exist_ok=True)
                path = out_root / f"part-{pd.Timestamp.now():%Y%m%d%H%M%S}.parquet"
                safe_path = str(path).replace("'", "''")
                conn.execute(f"COPY ({_dedup_sql(relation)}) TO '{safe_path}' (FORMAT PARQUET)")
                rows.append(check_row("create_clean_daily_price", "OK", f"created {path}", 1, 1))
            elif args.create_clean_dataset:
                rows.append(check_row("create_clean_daily_price", "DRY_RUN", "would create lake/clean_daily_price", 0, 1))
            if args.execute and args.dedupe_partitions:
                conn.close()
                stats = _dedupe_partitions(db_path, data_root)
                conn = duckdb.connect(str(db_path))
                relation = dataset_relation(conn, data_root, "daily_price")
                rows.append(
                    check_row(
                        "dedupe_daily_price_partitions",
                        "OK",
                        (
                            f"scanned={stats['partitions_scanned']} rewritten={stats['partitions_rewritten']} "
                            f"rows_before={stats['rows_before']} rows_after={stats['rows_after']}"
                        ),
                        stats["rows_before"] - stats["rows_after"],
                        0,
                    )
                )
                if relation:
                    rows.extend(_duplicate_report(conn, relation, suffix="_after_repair"))
            elif args.dedupe_partitions:
                rows.append(check_row("dedupe_daily_price_partitions", "DRY_RUN", "would rewrite duplicate daily_price partitions with backups", 0, 1))
        print_rows(rows)
        write_maintenance_rows(db_path, "database_repair_log", rows)
        print("saved: lake/database_repair_log")
    finally:
        conn.close()


def _duplicate_report(conn: duckdb.DuckDBPyConnection, relation: str, suffix: str = "") -> list[dict]:
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
    return [
        check_row(f"duplicate_keys_dry_run{suffix}", "WARN" if dup[0] else "OK", f"duplicate_keys={dup[0]}", dup[0], 0),
        check_row(f"duplicate_rows_dry_run{suffix}", "WARN" if dup[1] else "OK", f"duplicate_rows={dup[1]}", dup[1], 0),
    ]


def _dedup_sql(relation: str) -> str:
    return (
        "SELECT * FROM ("
        f"{relation}"
        ") QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY ts_code, trade_date, adjust_type "
        "ORDER BY source_priority DESC NULLS LAST, updated_at DESC NULLS LAST"
        ") = 1"
    )


def _dedupe_partitions(db_path: Path, data_root: Path) -> dict[str, int]:
    store = LakeStore(db_path=db_path, lake_root=data_root / "lake")
    try:
        store.connect()
        return store.dedupe_daily_price_partitions()
    finally:
        store.close()


if __name__ == "__main__":
    main()
