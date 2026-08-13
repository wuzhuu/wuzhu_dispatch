from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import (
    add_db_path_arg,
    ensure_project_dirs,
    load_settings,
    resolve_db_path,
)
from src.utils.proxy import disable_proxy_if_configured


REQUIRED_METADATA_TABLES = {
    "dataset_manifest",
    "ingestion_job",
    "ingestion_record",
    "symbol_coverage",
    "unique_key_index",
    "daily_update_log",
    "data_quality_log",
    "rss_news_articles",
    "stock_industry_map",
    "v_stock_industry_map",
    "v_industry_board",
    "v_industry_board_local",
    "data_source_status",
}


def _table_names(store: LakeStore) -> set[str]:
    try:
        rows = store.execute("SHOW TABLES").fetchall()
        return {row[0] for row in rows}
    except Exception:
        return set()


def _schema_needs_update(store: LakeStore) -> bool:
    existing = _table_names(store)
    return not REQUIRED_METADATA_TABLES.issubset(existing)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize or update the DuckDB database schema.")
    add_db_path_arg(parser)
    parser.add_argument("--force", action="store_true", help="Run DDL statements even if schema is up to date.")
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    ensure_project_dirs(settings)
    db_path = resolve_db_path(settings, args.db_path)
    store = LakeStore(db_path=db_path)
    try:
        exists = db_path.exists()
        needs_update = True if not exists else _schema_needs_update(store)
        if exists and not args.force and not needs_update:
            store.rebuild_manifest()
            store.refresh_views()
            print(f"DuckDB metadata schema is up to date, refreshed lake views: {db_path}")
            print("Use `python scripts/init_db.py --force` to run table creation statements anyway.")
            return
        store.init_metadata()
        store.rebuild_manifest()
        store.refresh_views()
        action = "updated" if exists else "initialized"
        print(f"DuckDB metadata {action}: {db_path}")
        print(f"Lake root: {store.lake_root}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
