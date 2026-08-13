from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.data_loader import StockDataLoader
from src.analysis.factors import calc_latest_factors
from src.analysis.risk import add_risk_flags
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze one stock from the local data lake.")
    parser.add_argument("ts_code")
    add_db_path_arg(parser)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    try:
        factors = add_risk_flags(calc_latest_factors(loader.get_stock_price(args.ts_code)))
        if factors.empty:
            raise RuntimeError(f"No price data found for {args.ts_code}")
        row = factors.iloc[0]
        fields = [
            ("ts_code", row.get("ts_code")),
            ("trade_date", row.get("trade_date")),
            ("close", row.get("close")),
            ("ret_20d", row.get("momentum_20d")),
            ("ret_60d", row.get("momentum_60d")),
            ("volatility_20d", row.get("volatility_20d")),
            ("max_drawdown_60d", row.get("drawdown_60d")),
            ("above_ma20", row.get("trend_ma20")),
            ("above_ma60", row.get("trend_ma60")),
            ("risk_level", row.get("risk_level")),
            ("risk_flags", row.get("risk_flags")),
        ]
        for key, value in fields:
            print(f"{key}: {value}")
        print(f"db_path: {loader.db_path}")
        print(f"tables_used: {loader.tables_used}")
    finally:
        loader.close()


if __name__ == "__main__":
    main()

