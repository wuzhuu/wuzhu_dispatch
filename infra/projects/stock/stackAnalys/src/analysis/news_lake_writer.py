from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.analysis.analysis_lake import AnalysisLakeStore


def append_dataset(db_path: str | Path, dataset: str, df: pd.DataFrame) -> list[Path]:
    if df is None or df.empty:
        store = AnalysisLakeStore(db_path)
        try:
            store.refresh_views([dataset])
        finally:
            store.close()
        return []
    store = AnalysisLakeStore(db_path)
    written: list[Path] = []
    try:
        out = df.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
        out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        for (year, month), part in out.groupby(["year", "month"], dropna=False):
            partition = store.lake_root / dataset / f"year={year}" / f"month={month}"
            partition.mkdir(parents=True, exist_ok=True)
            path = partition / f"part-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}.parquet"
            part.drop(columns=["year", "month"]).to_parquet(path, index=False)
            written.append(path)
        store.refresh_views([dataset])
        return written
    finally:
        store.close()


def refresh_news_llm_views(db_path: str | Path) -> None:
    datasets = [
        "news_llm_analysis",
        "llm_usage_log",
        "news_llm_stock_link",
        "news_llm_industry_link",
        "news_fused_analysis",
        "daily_news_llm_digest",
    ]
    store = AnalysisLakeStore(db_path)
    try:
        store.refresh_views(datasets)
    finally:
        store.close()
