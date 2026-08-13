from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.data_loader import StockDataLoader
from src.analysis.path_resolver import resolve_chart_root


@dataclass
class VisualContext:
    loader: StockDataLoader
    store: AnalysisLakeStore
    chart_root: Path
    trade_date: str


def build_context(db_path: str | Path | None = None, target_date: str | None = None) -> VisualContext:
    loader = StockDataLoader(db_path)
    store = AnalysisLakeStore(loader.db_path)
    chart_root = resolve_chart_root(loader.db_path)
    chart_root.mkdir(parents=True, exist_ok=True)
    trade_date = target_date or _latest_trade_date(store, loader)
    return VisualContext(loader=loader, store=store, chart_root=chart_root, trade_date=str(pd.to_datetime(trade_date).date()))


def _latest_trade_date(store: AnalysisLakeStore, loader: StockDataLoader) -> str:
    for dataset in ("score_daily", "market_state_daily"):
        df = store.read_dataset(dataset)
        if not df.empty and "trade_date" in df.columns:
            return str(pd.to_datetime(df["trade_date"], errors="coerce").max().date())
    return loader.get_latest_trade_date()
