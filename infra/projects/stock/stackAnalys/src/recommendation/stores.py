from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from src.analysis.analysis_lake import AnalysisLakeStore


RECOMMENDATION_DATASETS = [
    "recommendation_batch",
    "recommendation_item",
    "recommendation_universe",
    "recommendation_tracking_daily",
    "recommendation_item_result",
    "recommendation_batch_result",
    "recommendation_live_portfolio_daily",
    "recommendation_rolling_return_daily",
    "recommendation_evaluation_daily",
    "recommendation_explanation",
    "recommendation_stability_daily",
    "recommendation_list_change_detail",
    "recommendation_repair_log",
]


class RecommendationStore:
    def __init__(self, db_path: str | Path | None = None):
        self.store = AnalysisLakeStore(db_path)
        self.conn = self.store.connect()

    @property
    def db_path(self) -> Path:
        return self.store.db_path

    @property
    def data_root(self) -> Path:
        return self.store.data_root

    def close(self) -> None:
        self.store.close()

    def refresh(self) -> None:
        self.store.refresh_views(RECOMMENDATION_DATASETS)

    def write(self, dataset: str, rows: list[dict] | pd.DataFrame, skip_backup: bool | None = None) -> list[Path]:
        df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        if skip_backup is None:
            skip_backup = dataset.startswith("recommendation_")
        return self.store.write_dataset(dataset, df, skip_backup=skip_backup)

    def read_dataset(self, dataset: str) -> pd.DataFrame:
        self.store.refresh_views([dataset])
        return self.query_optional(f"SELECT * FROM v_{dataset}")

    def relation(self, *names: str) -> str | None:
        for name in names:
            if self.exists(name):
                return name
        return None

    def exists(self, name: str) -> bool:
        try:
            self.conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
            return True
        except Exception:
            return False

    def query_optional(self, sql: str, params: Iterable[object] | None = None) -> pd.DataFrame:
        try:
            return self.conn.execute(sql, list(params or [])).df()
        except (duckdb.Error, RuntimeError):
            return pd.DataFrame()

    def scalar_optional(self, sql: str, default=None):
        try:
            row = self.conn.execute(sql).fetchone()
            return row[0] if row else default
        except (duckdb.Error, RuntimeError):
            return default
