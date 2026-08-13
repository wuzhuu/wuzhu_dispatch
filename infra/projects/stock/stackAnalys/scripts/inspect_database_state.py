from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, write_maintenance_rows

PREFERRED_DB_NAMES = [
    "stock_data_v2.duckdb",
    "stock_data.duckdb",
    "metadata.duckdb",
]

MARKET_DATASETS = [
    "daily_price",
    "stock_basic",
    "index_daily",
    "factor_daily",
    "score_daily",
    "risk_flag_daily",
    "backtest_result",
    "backtest_monthly_returns",
    "backtest_holdings",
    "visualization_snapshot",
    "visualization_metric",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect local DuckDB/data-lake state without writing anything.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist", action="store_true", help="Do not write database_inspection records to the lake.")
    args = parser.parse_args()

    db_path = resolve_db_path(args.db_path)
    data_root = resolve_data_root(db_path)
    result = inspect_database(db_path, data_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(result)
    if not args.no_persist:
        write_maintenance_rows(db_path, "database_inspection", inspection_rows(result))
        print("saved: lake/database_inspection")


def resolve_db_path(cli_db_path: str | None) -> Path:
    if cli_db_path:
        return Path(cli_db_path).expanduser().resolve(strict=False)
    env_db = os.environ.get("STOCK_DB_PATH")
    if env_db:
        return Path(env_db).expanduser().resolve(strict=False)
    candidates = [p.resolve(strict=False) for p in REPO_ROOT.rglob("*.duckdb")]
    env_root = os.environ.get("STOCK_DATA_ROOT")
    preferred_roots = [Path(env_root).expanduser().resolve(strict=False)] if env_root else []
    preferred_roots.append(REPO_ROOT / "stock_local_ai_data")
    for root in preferred_roots:
        for name in PREFERRED_DB_NAMES:
            candidate = root / name
            if candidate.exists():
                return candidate.resolve(strict=False)
            candidate = root / "db" / name
            if candidate.exists():
                return candidate.resolve(strict=False)
    if candidates:
        return sorted(candidates, key=lambda p: (PREFERRED_DB_NAMES.index(p.name) if p.name in PREFERRED_DB_NAMES else 99, str(p)))[0]
    raise FileNotFoundError("No DuckDB database found under repository root.")


def resolve_data_root(db_path: Path) -> Path:
    env_root = os.environ.get("STOCK_DATA_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve(strict=False)
    if db_path.parent.name == "db":
        return db_path.parent.parent
    return db_path.parent


def inspect_database(db_path: Path, data_root: Path) -> dict[str, Any]:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        objects = _objects(conn)
        result: dict[str, Any] = {
            "project_root": str(PROJECT_ROOT),
            "repo_root": str(REPO_ROOT),
            "data_root": str(data_root),
            "stock_db_path": str(db_path),
            "objects": sorted(objects),
            "datasets": {},
            "report_artifacts": _report_artifacts(data_root),
            "project_report_artifacts": _project_report_artifacts(),
            "tmp_chart_scripts": [str(p) for p in Path("/tmp").glob("*chart*.py")] + [str(p) for p in Path("/tmp").glob("gen_charts*.py")],
            "chart_artifacts": [str(p) for p in (data_root / "artifacts" / "charts").glob("*.png")] if (data_root / "artifacts" / "charts").exists() else [],
        }
        for dataset in MARKET_DATASETS:
            result["datasets"][dataset] = _inspect_dataset(conn, objects, dataset, data_root)
        result["diagnostics"] = _diagnostics(conn, result)
        return result
    finally:
        conn.close()


def _objects(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
        """
    ).fetchall()
    return {row[0] for row in rows}


def _inspect_dataset(conn: duckdb.DuckDBPyConnection, objects: set[str], dataset: str, data_root: Path) -> dict[str, Any]:
    names = [f"v_{dataset}", dataset]
    found = next((name for name in names if name in objects), None)
    info: dict[str, Any] = {"exists": found is not None, "object": found}
    relation = found
    if found:
        try:
            _scalar(conn, f"SELECT COUNT(*) FROM {found}")
        except Exception as exc:
            info["object_error"] = repr(exc)
            relation = _parquet_relation(data_root, dataset)
    else:
        relation = _parquet_relation(data_root, dataset)
    if not relation:
        return info
    info["exists"] = True
    info["relation"] = relation
    info["row_count"] = _scalar(conn, f"SELECT COUNT(*) FROM {relation}")
    columns = _columns(conn, relation)
    info["columns"] = columns
    date_col = "trade_date" if "trade_date" in columns else "rebalance_date" if "rebalance_date" in columns else None
    if date_col:
        row = conn.execute(f"SELECT MIN({date_col}), MAX({date_col}) FROM {relation}").fetchone()
        info["min_date"] = row[0]
        info["max_date"] = row[1]
    if dataset == "daily_price":
        info["stock_count"] = _scalar(conn, f"SELECT COUNT(DISTINCT ts_code) FROM {relation}") if "ts_code" in columns else None
        if {"ts_code", "trade_date", "adjust_type"}.issubset(columns):
            info["duplicate_keys"] = _scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT ts_code, trade_date, adjust_type, COUNT(*) AS c
                    FROM {relation}
                    GROUP BY 1, 2, 3
                    HAVING COUNT(*) > 1
                )
                """,
            )
            info["duplicate_rows"] = _scalar(
                conn,
                f"""
                SELECT COALESCE(SUM(c - 1), 0)
                FROM (
                    SELECT ts_code, trade_date, adjust_type, COUNT(*) AS c
                    FROM {relation}
                    GROUP BY 1, 2, 3
                    HAVING COUNT(*) > 1
                )
                """,
            )
    if dataset == "stock_basic":
        for col in ("name", "industry", "market", "exchange"):
            if col in columns:
                info[f"{col}_missing_rate"] = _scalar(conn, f"SELECT AVG(CASE WHEN {col} IS NULL OR CAST({col} AS VARCHAR) = '' THEN 1.0 ELSE 0.0 END) FROM {relation}")
    if dataset in {"score_daily", "risk_flag_daily", "factor_daily"} and {"ts_code", "trade_date"}.issubset(columns):
        info["duplicate_keys"] = _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT ts_code, trade_date, COUNT(*) AS c
                FROM {relation}
                GROUP BY 1, 2
                HAVING COUNT(*) > 1
            )
            """,
        )
    return info


def _columns(conn: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = conn.execute(f"DESCRIBE SELECT * FROM {table_name} LIMIT 0").fetchall()
    return {row[0] for row in rows}


def _parquet_relation(data_root: Path, dataset: str) -> str | None:
    root = data_root / "lake" / dataset
    if not root.exists() or not any(root.rglob("*.parquet")):
        return None
    glob = (root / "**" / "*.parquet").as_posix().replace("'", "''")
    return f"(SELECT * FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true))"


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str):
    return conn.execute(sql).fetchone()[0]


def _report_artifacts(data_root: Path) -> list[str]:
    reports = data_root / "reports"
    if not reports.exists():
        return []
    return [str(p) for p in reports.rglob("*") if p.is_file()]


def _project_report_artifacts() -> list[str]:
    paths = []
    for rel in ("data/reports", "reports", "data/samples"):
        root = PROJECT_ROOT / rel
        if root.exists():
            paths.extend(str(p) for p in root.rglob("*") if p.is_file())
    return paths


def _diagnostics(conn: duckdb.DuckDBPyConnection, result: dict[str, Any]) -> dict[str, Any]:
    datasets = result["datasets"]
    diag = {
        "backtest_in_lake": datasets.get("backtest_result", {}).get("exists", False) and (datasets.get("backtest_result", {}).get("row_count") or 0) > 0,
        "visualization_in_lake": datasets.get("visualization_snapshot", {}).get("exists", False) and (datasets.get("visualization_snapshot", {}).get("row_count") or 0) > 0,
        "daily_price_duplicate_keys": datasets.get("daily_price", {}).get("duplicate_keys"),
        "industry_missing_rate": datasets.get("stock_basic", {}).get("industry_missing_rate"),
        "project_reports_exist": bool(result["project_report_artifacts"]),
        "data_root_reports_exist": bool(result["report_artifacts"]),
        "tmp_chart_scripts_exist": bool(result["tmp_chart_scripts"]),
        "charts_in_artifacts": bool(result["chart_artifacts"]),
    }
    diag["score_quantile_should_use_total_score"] = datasets.get("score_daily", {}).get("exists", False)
    index_info = datasets.get("index_daily", {})
    diag["index_daily_has_history"] = bool(index_info.get("row_count", 0))
    diag["index_daily_history_sufficient"] = bool((index_info.get("row_count") or 0) >= 120)
    return diag


def print_human(result: dict[str, Any]) -> None:
    print(f"PROJECT_ROOT={result['project_root']}")
    print(f"DATA_ROOT={result['data_root']}")
    print(f"STOCK_DB_PATH={result['stock_db_path']}")
    print("")
    print("Core objects:")
    for name in result["objects"]:
        print(f"- {name}")
    print("")
    print("Datasets:")
    for name, info in result["datasets"].items():
        print(f"- {name}: exists={info.get('exists')} object={info.get('object')} relation={info.get('relation')} rows={info.get('row_count')} range={info.get('min_date')}..{info.get('max_date')}")
        if info.get("object_error"):
            print(f"  object_error: {info['object_error']}")
        for key in ("stock_count", "duplicate_keys", "duplicate_rows", "name_missing_rate", "industry_missing_rate", "market_missing_rate", "exchange_missing_rate"):
            if key in info:
                print(f"  {key}: {info[key]}")
    print("")
    print("Artifacts:")
    print(f"- DATA_ROOT/reports files: {len(result['report_artifacts'])}")
    print(f"- PROJECT_ROOT report/sample files: {len(result['project_report_artifacts'])}")
    print(f"- /tmp chart scripts: {len(result['tmp_chart_scripts'])}")
    print(f"- DATA_ROOT/artifacts/charts png: {len(result['chart_artifacts'])}")
    print("")
    print("Diagnostics:")
    for key, value in result["diagnostics"].items():
        print(f"- {key}: {value}")


def inspection_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, info in result["datasets"].items():
        rows.append(check_row(f"{name}_exists", "OK" if info.get("exists") else "FAIL", f"rows={info.get('row_count')}", info.get("row_count"), 1))
        for key in ("duplicate_keys", "duplicate_rows", "stock_count", "industry_missing_rate", "market_missing_rate"):
            if key in info:
                status = "FAIL" if key.startswith("duplicate") and info[key] else "OK"
                rows.append(check_row(f"{name}_{key}", status, f"{key}={info[key]}", info[key], 0))
    for key, value in result["diagnostics"].items():
        rows.append(check_row(key, "OK" if not (key.endswith("exist") and value is False) else "WARN", str(value), value, None))
    return rows


if __name__ == "__main__":
    main()
