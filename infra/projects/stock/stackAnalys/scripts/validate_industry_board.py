from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate local industry board data and v_industry_board.")
    add_db_path_arg(parser)
    parser.add_argument("--dry-run", action="store_true", help="Run checks without writing validation parquet.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    db_path = Path(resolve_db_path(settings, args.db_path)).expanduser().resolve(strict=False)
    validation_id = uuid.uuid4().hex
    store = LakeStore(db_path=db_path)
    rows: list[dict[str, object]] = []
    try:
        store.connect()
        store.refresh_views()
        ctx = {"validation_id": validation_id, "db_path": str(db_path), "data_root": str(store.lake_root.parent)}
        rows.extend(run_checks(store, ctx))
        df = pd.DataFrame(rows)
        print(df[["check_name", "status", "issue_level", "message", "value", "threshold"]].to_string(index=False))
        if not args.dry_run:
            written = store.write_industry_validation(df)
            print(f"validation rows written: {written}")
            print(f"lake_root: {store.lake_root}")
    finally:
        store.close()


def run_checks(store: LakeStore, ctx: dict[str, str]) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    checks.append(check_exists(store, ctx, "v_stock_industry_map"))
    map_rows = scalar(store, "SELECT COUNT(*) FROM v_stock_industry_map")
    checks.append(row(ctx, "industry_map_rows", "OK" if map_rows and map_rows > 0 else "WARN", "WARNING" if not map_rows else "INFO", "stock industry map row count", map_rows, ">0"))

    coverage = query(
        store,
        """
        WITH stocks AS (SELECT DISTINCT ts_code FROM v_daily_price),
        mapped AS (SELECT DISTINCT ts_code FROM v_stock_industry_map WHERE industry_name IS NOT NULL)
        SELECT COUNT(mapped.ts_code) * 1.0 / NULLIF(COUNT(stocks.ts_code), 0) AS coverage
        FROM stocks LEFT JOIN mapped USING(ts_code)
        """,
    )
    coverage_value = None if coverage.empty else coverage.iloc[0, 0]
    checks.append(row(ctx, "industry_map_coverage", "OK" if pd.notna(coverage_value) and float(coverage_value) >= 0.8 else "WARN", "WARNING", "stock mapping coverage ratio", coverage_value, ">=0.8"))

    checks.append(check_exists(store, ctx, "v_industry_board_local"))
    checks.append(check_exists(store, ctx, "v_industry_board"))
    board_rows = scalar(store, "SELECT COUNT(*) FROM v_industry_board")
    checks.append(row(ctx, "v_industry_board_rows", "OK" if board_rows and board_rows > 0 else "WARN", "WARNING", "v_industry_board readable row count", board_rows, ">0"))

    date_range = query(store, "SELECT MIN(trade_date) AS min_date, MAX(trade_date) AS max_date FROM v_industry_board")
    if not date_range.empty:
        value = f"{date_range['min_date'].iloc[0]}..{date_range['max_date'].iloc[0]}"
    else:
        value = ""
    checks.append(row(ctx, "industry_board_date_range", "OK" if board_rows else "WARN", "INFO" if board_rows else "WARNING", "industry_board_local date range", value, "non-empty"))

    daily_counts = query(
        store,
        """
        SELECT trade_date, COUNT(*) AS industry_count, MAX(unmapped_count) AS unmapped_count,
               MIN(valid_member_count) AS min_valid_members, MEDIAN(valid_member_count) AS median_valid_members
        FROM v_industry_board
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 10
        """,
    )
    checks.append(row(ctx, "daily_industry_counts", "OK" if not daily_counts.empty else "WARN", "INFO" if not daily_counts.empty else "WARNING", "latest daily industry count snapshot", daily_counts.to_json(orient="records", force_ascii=False, date_format="iso"), "available"))

    dupes = scalar(
        store,
        """
        SELECT COUNT(*)
        FROM (
            SELECT trade_date, industry_source, industry_level, industry_code, COUNT(*) AS c
            FROM v_industry_board
            GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) > 1
        )
        """,
    )
    checks.append(row(ctx, "duplicate_industry_board_keys", "OK" if int(dupes or 0) == 0 else "FAIL", "ERROR" if int(dupes or 0) else "INFO", "duplicate trade_date+source+level+code keys", dupes, "0"))

    unreasonable = scalar(store, "SELECT COUNT(*) FROM v_industry_board WHERE ABS(avg_ret_1d) > 1 OR ABS(median_ret_1d) > 1")
    checks.append(row(ctx, "return_range", "OK" if int(unreasonable or 0) == 0 else "WARN", "WARNING" if int(unreasonable or 0) else "INFO", "avg/median daily returns outside +/-100%", unreasonable, "0"))

    negative_amount = scalar(store, "SELECT COUNT(*) FROM v_industry_board WHERE amount_sum < 0")
    checks.append(row(ctx, "amount_sum_non_negative", "OK" if int(negative_amount or 0) == 0 else "FAIL", "ERROR" if int(negative_amount or 0) else "INFO", "negative amount_sum rows", negative_amount, "0"))

    legacy_refs = scan_legacy_refs()
    checks.append(row(ctx, "legacy_eastmoney_industry_refs", "OK" if not legacy_refs else "WARN", "WARNING" if legacy_refs else "INFO", "code paths still mentioning old EastMoney industry_board dependencies", "; ".join(legacy_refs[:20]), "none in production paths"))

    rec_status = recommendation_smoke(store)
    checks.append(row(ctx, "recommendation_industry_read", "OK" if rec_status.startswith("OK") else "WARN", "WARNING", rec_status, board_rows, "warning not failure when missing"))
    return checks


def check_exists(store: LakeStore, ctx: dict[str, str], relation: str) -> dict[str, object]:
    exists = False
    try:
        store.execute(f"SELECT 1 FROM {relation} LIMIT 1").fetchone()
        exists = True
    except Exception:
        exists = False
    return row(ctx, f"{relation}_exists", "OK" if exists else "FAIL", "ERROR" if not exists else "INFO", f"{relation} is readable", exists, "true")


def recommendation_smoke(store: LakeStore) -> str:
    try:
        rel_rows = scalar(store, "SELECT COUNT(*) FROM v_industry_board")
        if not rel_rows:
            return "OK: v_industry_board empty; recommendation should use missing/neutral industry strength"
        cols = query(store, "DESCRIBE SELECT * FROM v_industry_board LIMIT 0")
        colset = set(cols["column_name"].astype(str)) if not cols.empty else set()
        needed = {"trade_date", "industry_name", "industry_strength_score", "source"}
        missing = needed - colset
        if missing:
            return f"WARN: v_industry_board missing columns {sorted(missing)}"
        return "OK: v_industry_board exposes industry strength columns"
    except Exception as exc:
        return f"WARN: recommendation smoke check could not read industry view: {exc!r}"


def scan_legacy_refs() -> list[str]:
    patterns = (
        "fetch_industry_boards",
        "stock_board_industry_name_em",
        "v_industry_board_latest",
        '"industry_board"',
        "'industry_board'",
    )
    refs = []
    for root in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for lineno, text in enumerate(lines, start=1):
                if any(pattern in text for pattern in patterns):
                    refs.append(f"{rel}:{lineno}:{text}")
    filtered = []
    for line in refs:
        if "validate_industry_board.py" in line or "build_industry_board_local.py" in line:
            continue
        if "scripts/test_" in line or "/scripts/test_" in line:
            continue
        if "src/storage/lake_store.py" in line:
            continue
        if "src/collectors/sector_data.py" in line:
            continue
        if "write_industry_board_snapshot" in line or "INDUSTRY_BOARD_COLUMNS" in line:
            continue
        filtered.append(line)
    return filtered


def query(store: LakeStore, sql: str) -> pd.DataFrame:
    try:
        return store.execute(sql).df()
    except Exception:
        return pd.DataFrame()


def scalar(store: LakeStore, sql: str):
    try:
        result = store.execute(sql).fetchone()
        return result[0] if result else None
    except Exception:
        return None


def row(ctx: dict[str, str], check_name: str, status: str, issue_level: str, message: str, value, threshold) -> dict[str, object]:
    now = pd.Timestamp.now()
    return {
        "validation_id": ctx["validation_id"],
        "validated_at": now,
        "db_path": ctx["db_path"],
        "data_root": ctx["data_root"],
        "check_name": check_name,
        "status": status,
        "issue_level": issue_level,
        "message": message,
        "value": "" if value is None else str(value),
        "threshold": str(threshold),
        "created_at": now,
    }


if __name__ == "__main__":
    main()
