from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.data_loader import StockDataLoader
from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.report import build_stock_report
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one stock Markdown report.")
    parser.add_argument("ts_code")
    add_db_path_arg(parser)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    store = AnalysisLakeStore(loader.db_path)
    try:
        row = build_stock_report(args.ts_code, loader)
        loader.close()
        store.write_dataset("stock_report", pd.DataFrame([row]))
        print("saved: lake/stock_report")
        print("query:")
        print("SELECT report_markdown")
        print("FROM v_stock_report")
        print(f"WHERE ts_code = '{args.ts_code}'")
        print("ORDER BY trade_date DESC")
        print("LIMIT 1;")
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
