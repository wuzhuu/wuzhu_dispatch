from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path
from src.utils.proxy import disable_proxy_if_configured


DEFAULT_LEGACY_DB = PROJECT_ROOT.parent / "stock_local_ai_data" / "stock_data.duckdb"
LOG_TABLES = ("daily_update_log", "data_quality_log", "rss_news_articles")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the current DuckDB metadata and Parquet lake from a legacy DuckDB database.",
    )
    parser.add_argument(
        "--legacy-db",
        default=str(DEFAULT_LEGACY_DB),
        help="Path to the legacy DuckDB database that contains entity tables.",
    )
    add_db_path_arg(parser)
    parser.add_argument(
        "--lake-root",
        default=None,
        help="Target Parquet lake root. Defaults to data.lake_root in config/settings.yaml.",
    )
    parser.add_argument(
        "--stock-snapshot-date",
        default=None,
        help="Snapshot date to assign to legacy stock_basic rows. Defaults to today.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Remove the target DuckDB file and target lake before rebuilding.",
    )
    return parser.parse_args()


def table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def table_count(conn: duckdb.DuckDBPyConnection, table_name: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])


def reset_target(db_path: Path, lake_root: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    if lake_root.exists():
        shutil.rmtree(lake_root)


def import_daily_price(source: duckdb.DuckDBPyConnection, store: LakeStore) -> int:
    if not table_exists(source, "daily_price"):
        return 0
    partitions = source.execute(
        """
        SELECT DISTINCT STRFTIME(trade_date, '%Y') AS year, STRFTIME(trade_date, '%m') AS month
        FROM daily_price
        WHERE trade_date IS NOT NULL
        ORDER BY year, month
        """
    ).fetchall()
    total = 0
    for year, month in partitions:
        df = source.execute(
            """
            SELECT *
            FROM daily_price
            WHERE STRFTIME(trade_date, '%Y') = ? AND STRFTIME(trade_date, '%m') = ?
            """,
            [year, month],
        ).df()
        if df.empty:
            continue
        total += store.upsert_daily_price(df, adjust_type="qfq")
        print(f"daily_price {year}-{month}: {len(df)} rows")
    return total


def import_index_daily(source: duckdb.DuckDBPyConnection, store: LakeStore) -> int:
    if not table_exists(source, "index_daily"):
        return 0
    df = source.execute("SELECT * FROM index_daily").df()
    if df.empty:
        return 0
    rows = store.upsert_index_daily(df)
    print(f"index_daily: {len(df)} rows")
    return rows


def import_stock_basic(source: duckdb.DuckDBPyConnection, store: LakeStore, snapshot_date: str | None) -> int:
    if not table_exists(source, "stock_basic"):
        return 0
    df = source.execute("SELECT * FROM stock_basic").df()
    if df.empty:
        return 0
    rows = store.write_stock_basic_snapshot(df, snapshot_date=snapshot_date)
    print(f"stock_basic: {len(df)} rows")
    return rows


def copy_log_tables(source: duckdb.DuckDBPyConnection, store: LakeStore) -> dict[str, int]:
    copied: dict[str, int] = {}
    target = store.connect()
    for table_name in LOG_TABLES:
        if not table_exists(source, table_name):
            continue
        df = source.execute(f'SELECT * FROM "{table_name}"').df()
        if df.empty:
            copied[table_name] = 0
            continue
        target.register("tmp_legacy_log", df)
        try:
            target.execute(f'INSERT INTO "{table_name}" SELECT * FROM tmp_legacy_log')
        finally:
            target.unregister("tmp_legacy_log")
        copied[table_name] = len(df)
        print(f"{table_name}: {len(df)} rows")
    return copied


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    legacy_db = Path(args.legacy_db).expanduser().resolve()
    db_path = resolve_db_path(settings, args.db_path).resolve()
    store = LakeStore(db_path=db_path, lake_root=args.lake_root)

    if not legacy_db.exists():
        raise SystemExit(f"Legacy database does not exist: {legacy_db}")
    if legacy_db == db_path:
        raise SystemExit("Legacy database and target database must be different files.")
    if args.replace:
        reset_target(db_path, store.lake_root)

    source = duckdb.connect(str(legacy_db), read_only=True)
    try:
        store.connect()
        totals = {
            "daily_price": import_daily_price(source, store),
            "index_daily": import_index_daily(source, store),
            "stock_basic": import_stock_basic(source, store, args.stock_snapshot_date),
        }
        log_totals = copy_log_tables(source, store)
        store.rebuild_manifest()
        store.refresh_views()
        print("Rebuild complete.")
        print(f"Legacy DB: {legacy_db}")
        print(f"Target DB: {db_path}")
        print(f"Lake root: {store.lake_root}")
        print(f"Imported entity rows: {totals}")
        print(f"Copied log rows: {log_totals}")
        for table_name in ("dataset_manifest", "symbol_coverage", "unique_key_index"):
            print(f"{table_name}: {table_count(store.connect(), table_name)} rows")
    finally:
        source.close()
        store.close()


if __name__ == "__main__":
    main()
