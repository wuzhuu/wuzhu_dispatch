from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.backtest import backtest_monthly_top_n
from src.analysis.data_loader import StockDataLoader
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest monthly top-N factor ranking.")
    add_db_path_arg(parser)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    store = AnalysisLakeStore(loader.db_path)
    try:
        price = loader.get_market_price(args.start, args.end)
        result = backtest_monthly_top_n(price, top_n=args.top_n, start_date=args.start, end_date=args.end)
        strategy_name = f"monthly_top_{args.top_n}"
        start_label = args.start or str(pd.to_datetime(price["trade_date"]).min().date())
        end_label = args.end or str(pd.to_datetime(price["trade_date"]).max().date())
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{strategy_name}:{start_label}:{end_label}").hex
        monthly = result["monthly_returns"].copy()
        holdings = result["holdings"].copy()
        summary = result["summary"]
        extra = _risk_metrics(monthly)
        summary_row = {
            "run_id": run_id,
            "strategy_name": strategy_name,
            "start_date": start_label,
            "end_date": end_label,
            "top_n": args.top_n,
            "benchmark": "hs300",
            "total_return": summary.get("total_return"),
            "annual_return": summary.get("annual_return"),
            "max_drawdown": summary.get("max_drawdown"),
            "win_rate": summary.get("win_rate"),
            "annual_volatility": extra["annual_volatility"],
            "sharpe": extra["sharpe"],
            "calmar": extra["calmar"],
            "benchmark_return": None,
            "excess_return": None,
            "summary_json": json.dumps(
                {
                    "summary": summary,
                    "monthly_returns": monthly.to_dict(orient="records"),
                    "equity_curve": result["equity_curve"].to_dict(orient="records"),
                },
                ensure_ascii=False,
                default=str,
            ),
            "created_at": pd.Timestamp.now(),
        }
        if not holdings.empty:
            holdings["run_id"] = run_id
            holdings["strategy_name"] = strategy_name
            holdings["holding_month"] = holdings.get("period")
            holdings["rank"] = holdings.groupby("rebalance_date").cumcount() + 1
            holdings["weight"] = 1.0 / args.top_n
            basic = loader.get_stock_basic()
            keep = [col for col in ["ts_code", "name", "industry"] if col in basic.columns]
            if keep:
                holdings = holdings.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
            holdings["score"] = pd.NA
            holdings["created_at"] = pd.Timestamp.now()
        if not monthly.empty:
            monthly["run_id"] = run_id
            monthly["strategy_name"] = strategy_name
            monthly["created_at"] = pd.Timestamp.now()
        loader.close()
        store.write_dataset("backtest_result", pd.DataFrame([summary_row]))
        store.write_dataset("backtest_monthly_returns", monthly)
        store.write_dataset("backtest_holdings", holdings)
        for key, value in summary.items():
            print(f"{key}: {value}")
        print("saved: lake/backtest_result, lake/backtest_monthly_returns, lake/backtest_holdings")
        print("query: SELECT * FROM v_backtest_result ORDER BY created_at DESC LIMIT 5;")
        print(f"data_root: {store.data_root}")
        print(f"db_path: {loader.db_path}")
        print(f"tables_used: {loader.tables_used}")
    finally:
        store.close()
        try:
            loader.close()
        except Exception:
            pass


def _risk_metrics(monthly: pd.DataFrame) -> dict[str, float | None]:
    if monthly.empty:
        return {"annual_volatility": None, "sharpe": None, "calmar": None}
    returns = pd.to_numeric(monthly["return"], errors="coerce").dropna()
    equity = pd.to_numeric(monthly["equity"], errors="coerce")
    annual_volatility = float(returns.std() * (12 ** 0.5)) if len(returns) > 1 else 0.0
    annual_return = float(equity.iloc[-1] ** (12 / len(monthly)) - 1) if len(monthly) else 0.0
    drawdown = float((equity / equity.cummax() - 1).min()) if not equity.empty else 0.0
    return {
        "annual_volatility": annual_volatility,
        "sharpe": annual_return / annual_volatility if annual_volatility else None,
        "calmar": annual_return / abs(drawdown) if drawdown else None,
    }


if __name__ == "__main__":
    main()
