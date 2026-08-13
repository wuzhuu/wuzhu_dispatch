from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.data_loader import StockDataLoader
from src.analysis.factors import calc_latest_factors
from src.analysis.market_state import analyze_market_state
from src.analysis.risk import add_risk_flags
from src.analysis.scoring import score_stocks
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local analysis pipeline and persist all outputs to the data lake.")
    add_db_path_arg(parser)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--with-visuals", action="store_true")
    parser.add_argument("--with-news-analysis", action="store_true")
    parser.add_argument("--with-llm-news", action="store_true")
    parser.add_argument("--llm-config", default="../key/stackAnalys_llm.yaml")
    parser.add_argument("--llm-limit", type=int, default=20)
    args = parser.parse_args()
    loader = StockDataLoader(args.db_path)
    store = AnalysisLakeStore(loader.db_path)
    try:
        if args.with_news_analysis:
            try:
                from scripts.build_news_analysis import build_news_analysis

                result = build_news_analysis(loader.db_path)
                for message in result.get("messages", []):
                    if str(message).startswith("WARNING"):
                        print(message)
            except Exception as exc:
                print(f"WARNING: news analysis failed and analysis pipeline will continue: {exc}")
        state = analyze_market_state(loader)
        price = loader.get_market_price(start_date=args.start, end_date=state["latest_trade_date"])
        factors = calc_latest_factors(price)
        ranked = add_risk_flags(score_stocks(factors))
        basic = loader.get_stock_basic()
        keep = [col for col in ["ts_code", "name", "industry", "market"] if col in basic.columns]
        if keep:
            ranked = ranked.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
        loader.close()
        store.write_dataset("factor_daily", factors)
        store.write_dataset("score_daily", ranked)
        risk_cols = [col for col in ["ts_code", "trade_date", "risk_level", "risk_flags", "rank", "total_score", "created_at"] if col in ranked.columns]
        store.write_dataset("risk_flag_daily", ranked[risk_cols].copy())
        store.write_dataset("market_state_daily", pd.DataFrame([{**state, "trade_date": state["latest_trade_date"]}]))
        store.write_dataset("analysis_universe", _analysis_universe(ranked))
        report_markdown = _daily_report(state, ranked, loader.db_path)
        store.write_dataset(
            "daily_analysis_report",
            pd.DataFrame(
                [
                    {
                        "trade_date": state["latest_trade_date"],
                        "report_type": "daily_analysis",
                        "report_version": "v1",
                        "market_regime": state["market_regime"],
                        "summary_json": json.dumps({"market_state": state}, ensure_ascii=False, default=str),
                        "report_markdown": report_markdown,
                        "created_at": pd.Timestamp.now(),
                    }
                ]
            ),
        )
        print(f"latest_trade_date: {state['latest_trade_date']}")
        print(f"factor_daily rows: {len(factors)}")
        print(f"score_daily rows: {len(ranked)}")
        print(f"data_root: {store.data_root}")
        print("query: SELECT * FROM v_score_daily ORDER BY trade_date DESC, rank ASC LIMIT 20;")
        if args.with_llm_news:
            try:
                from scripts.analyze_news_with_llm import analyze_news_with_llm
                from scripts.generate_daily_news_digest import generate_digest

                for message in analyze_news_with_llm(loader.db_path, args.llm_config, target_date=state["latest_trade_date"], limit=args.llm_limit):
                    print(f"news_llm: {message}")
                for message in generate_digest(loader.db_path, args.llm_config, target_date=state["latest_trade_date"]):
                    print(f"news_digest: {message}")
            except Exception as exc:
                print(f"WARNING: LLM news step failed and analysis pipeline remains complete: {exc}")
        if args.with_visuals:
            from src.visualization.dashboard_summary import build_visual_dashboard

            build_visual_dashboard(loader.db_path, target_date=state["latest_trade_date"])
    finally:
        store.close()
        try:
            loader.close()
        except Exception:
            pass


def _analysis_universe(ranked: pd.DataFrame) -> pd.DataFrame:
    cols = [col for col in ["ts_code", "trade_date", "rank", "total_score", "risk_level"] if col in ranked.columns]
    out = ranked[cols].copy()
    out["universe_name"] = "daily_rank_universe"
    out["included"] = True
    out["created_at"] = pd.Timestamp.now()
    return out


def _daily_report(state: dict, ranked: pd.DataFrame, db_path: Path) -> str:
    top_cols = [col for col in ["rank", "ts_code", "name", "industry", "total_score", "risk_level"] if col in ranked.columns]
    return "\n".join(
        [
            "# Daily Analysis",
            "",
            f"- latest_trade_date: {state['latest_trade_date']}",
            f"- market_regime: {state['market_regime']}",
            f"- stock_count: {state['stock_count']}",
            f"- db_path: {db_path}",
            "",
            "## Top 20",
            "",
            ranked[top_cols].head(20).to_markdown(index=False),
        ]
    ) + "\n"


if __name__ == "__main__":
    main()
