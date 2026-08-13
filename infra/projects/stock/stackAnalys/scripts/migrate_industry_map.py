from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1].expanduser().resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy stock_basic.industry values into stock_industry_map.")
    add_db_path_arg(parser)
    parser.add_argument("--dry-run", action="store_true", help="Only print candidate counts; do not write stock_industry_map.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    db_path = Path(resolve_db_path(settings, args.db_path)).expanduser().resolve()
    rows = extract_legacy_industry_rows(db_path)
    print(f"legacy industry rows: {len(rows)}")
    if args.dry_run:
        return
    store = LakeStore(db_path=db_path)
    try:
        store.init_metadata()
        written = store.upsert_stock_industry_map(rows)
        print(f"stock_industry_map upserted rows: {written}")
    finally:
        store.close()


def extract_legacy_industry_rows(db_path: Path) -> pd.DataFrame:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        relation = _stock_basic_relation(conn)
        if not relation:
            return _empty()
        cols = {row[0] for row in conn.execute(f"DESCRIBE SELECT * FROM {relation} LIMIT 0").fetchall()}
        if "industry" not in cols or "ts_code" not in cols:
            return _empty()
        order_sql = "updated_at DESC NULLS LAST" if "updated_at" in cols else "ts_code"
        df = conn.execute(
            f"""
            SELECT
                ts_code,
                industry AS industry_name,
                CAST(NULL AS TEXT) AS sw_code_2021,
                'legacy_stock_basic' AS source,
                NOW() AS fetched_at
            FROM {relation}
            WHERE industry IS NOT NULL
              AND TRIM(CAST(industry AS TEXT)) NOT IN ('', '-', '--', 'None', 'nan', '<NA>')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY {order_sql}) = 1
            """
        ).df()
        return df.drop_duplicates("ts_code", keep="first").reset_index(drop=True)
    finally:
        conn.close()


def _stock_basic_relation(conn: duckdb.DuckDBPyConnection) -> str | None:
    for name in ("v_stock_basic_latest", "stock_basic"):
        try:
            conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
            return name
        except Exception:
            continue
    return None


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts_code", "industry_name", "sw_code_2021", "source", "fetched_at"])


if __name__ == "__main__":
    main()
