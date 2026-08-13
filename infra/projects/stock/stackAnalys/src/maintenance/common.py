from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.analysis.analysis_lake import AnalysisLakeStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent
PREFERRED_DB_NAMES = ["stock_data_v2.duckdb", "stock_data.duckdb", "metadata.duckdb"]


def resolve_db_path(cli_db_path: str | None = None) -> Path:
    if cli_db_path:
        return Path(cli_db_path).expanduser().resolve(strict=False)
    env_db = os.environ.get("STOCK_DB_PATH")
    if env_db:
        return Path(env_db).expanduser().resolve(strict=False)
    env_root = os.environ.get("STOCK_DATA_ROOT")
    roots = [Path(env_root).expanduser().resolve(strict=False)] if env_root else []
    roots.append(REPO_ROOT / "stock_local_ai_data")
    for root in roots:
        for name in PREFERRED_DB_NAMES:
            for candidate in (root / name, root / "db" / name):
                if candidate.exists():
                    return candidate.resolve(strict=False)
    candidates = [p.resolve(strict=False) for p in REPO_ROOT.rglob("*.duckdb")]
    if candidates:
        return sorted(candidates, key=lambda p: (PREFERRED_DB_NAMES.index(p.name) if p.name in PREFERRED_DB_NAMES else 99, str(p)))[0]
    raise FileNotFoundError("No DuckDB database found.")


def resolve_data_root(db_path: str | Path) -> Path:
    env_root = os.environ.get("STOCK_DATA_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve(strict=False)
    path = Path(db_path).expanduser().resolve(strict=False)
    if path.parent.name == "db":
        return path.parent.parent
    return path.parent


def table_or_view_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def parquet_relation(data_root: Path, dataset: str) -> str | None:
    root = data_root / "lake" / dataset
    if not root.exists() or not any(root.rglob("*.parquet")):
        return None
    glob = (root / "**" / "*.parquet").as_posix().replace("'", "''")
    return f"(SELECT * FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true))"


def dataset_relation(conn: duckdb.DuckDBPyConnection, data_root: Path, dataset: str) -> str | None:
    for name in (f"v_{dataset}", dataset):
        if table_or_view_exists(conn, name):
            return name
    return parquet_relation(data_root, dataset)


def columns(conn: duckdb.DuckDBPyConnection, relation: str) -> set[str]:
    rows = conn.execute(f"DESCRIBE SELECT * FROM {relation} LIMIT 0").fetchall()
    return {row[0] for row in rows}


def check_row(
    check_name: str,
    status: str,
    message: str,
    value: Any = None,
    threshold: Any = None,
    trade_date: Any = None,
) -> dict[str, Any]:
    return {
        "trade_date": pd.to_datetime(trade_date).date() if trade_date is not None else pd.Timestamp.now().date(),
        "check_name": check_name,
        "status": status,
        "message": message,
        "value": None if value is None else str(value),
        "threshold": None if threshold is None else str(threshold),
        "created_at": pd.Timestamp.now(),
    }


def write_maintenance_rows(db_path: Path, dataset: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    store = AnalysisLakeStore(db_path=db_path)
    try:
        store.write_dataset(dataset, pd.DataFrame(rows))
    finally:
        store.close()


def print_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(f"{row['check_name']}: {row['status']} {row['message']}")
