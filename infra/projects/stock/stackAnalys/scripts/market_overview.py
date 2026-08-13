from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.data_loader import StockDataLoader
from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.market_state import analyze_market_state
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a market overview from local data.")
    add_db_path_arg(parser)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    store = AnalysisLakeStore(loader.db_path)
    try:
        state = analyze_market_state(loader)
        for key, value in state.items():
            print(f"{key}: {value}")
        df = pd.DataFrame([{**state, "trade_date": state["latest_trade_date"]}])
        loader.close()
        store.write_dataset("market_state_daily", df)
        print("saved: lake/market_state_daily")
        print("query: SELECT * FROM v_market_state_daily ORDER BY trade_date DESC LIMIT 1;")
        print(f"data_root: {store.data_root}")
        print(f"db_path: {loader.db_path}")
        print(f"tables_used: {loader.tables_used}")
    finally:
        store.close()
        try:
            loader.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
