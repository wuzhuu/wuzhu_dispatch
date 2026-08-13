from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.data_loader import StockDataLoader
from src.analysis.factors import calc_latest_factors
from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.risk import add_risk_flags
from src.analysis.scoring import score_stocks
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank stocks by local technical factors.")
    add_db_path_arg(parser)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    store = AnalysisLakeStore(loader.db_path)
    try:
        factors = calc_latest_factors(loader.get_market_price(args.start, args.end))
        scored = score_stocks(factors)
        ranked = add_risk_flags(scored)
        basic = loader.get_stock_basic()
        keep = [col for col in ["ts_code", "name", "industry", "market"] if col in basic.columns]
        if keep:
            ranked = ranked.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
        loader.close()
        store.write_dataset("factor_daily", factors)
        store.write_dataset("score_daily", ranked)
        risk_cols = [col for col in ["ts_code", "trade_date", "risk_level", "risk_flags", "rank", "total_score", "created_at"] if col in ranked.columns]
        store.write_dataset("risk_flag_daily", ranked[risk_cols].copy())
        print(ranked.head(args.top).to_string(index=False))
        print("saved: lake/factor_daily, lake/score_daily, lake/risk_flag_daily")
        print("query: SELECT * FROM v_score_daily ORDER BY trade_date DESC, rank ASC LIMIT 20;")
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
