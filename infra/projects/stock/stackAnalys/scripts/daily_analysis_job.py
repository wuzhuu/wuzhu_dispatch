from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
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
    parser = argparse.ArgumentParser(description="Run read-only daily analysis after data update.")
    add_db_path_arg(parser)
    parser.add_argument("--with-visuals", action="store_true", help="Also build visualization dashboard after analysis.")
    parser.add_argument("--with-news-analysis", action="store_true", help="Optionally build RSS news analysis before writing the daily report.")
    parser.add_argument("--with-llm-news", action="store_true", help="Optionally run LLM news extraction and digest after market analysis.")
    parser.add_argument("--llm-config", default="../key/stackAnalys_llm.yaml")
    parser.add_argument("--llm-limit", type=int, default=20)
    parser.add_argument("--recommendation-config", default="config/recommendation.example.yaml")
    parser.add_argument("--skip-recommendation-evaluation", action="store_true")
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
                print(f"WARNING: news analysis failed and daily market analysis will continue: {exc}")
        state = analyze_market_state(loader)
        factors = calc_latest_factors(loader.get_market_price(start_date="2025-01-01"))
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
        if not args.skip_recommendation_evaluation:
            _run_recommendation_evaluation(loader.db_path, args.recommendation_config, state["latest_trade_date"])
        report_markdown, main_risks = _daily_report_markdown(state, ranked, loader.db_path)
        store.write_dataset(
            "daily_analysis_report",
            pd.DataFrame(
                [
                    {
                        "trade_date": state["latest_trade_date"],
                        "report_type": "daily_analysis",
                        "report_version": "v1",
                        "market_regime": state["market_regime"],
                        "summary_json": json.dumps({"market_state": state, "main_risks": main_risks}, ensure_ascii=False, default=str),
                        "report_markdown": report_markdown,
                        "created_at": pd.Timestamp.now(),
                    }
                ]
            ),
        )
        print(f"latest_trade_date: {state['latest_trade_date']}")
        print(f"market_regime: {state['market_regime']}")
        print("saved: lake/factor_daily, lake/score_daily, lake/risk_flag_daily, lake/market_state_daily, lake/daily_analysis_report")
        print("query: SELECT report_markdown FROM v_daily_analysis_report ORDER BY trade_date DESC LIMIT 1;")
        print(f"data_root: {store.data_root}")
        print(f"db_path: {loader.db_path}")
        print(f"tables_used: {loader.tables_used}")
        if args.with_llm_news:
            try:
                from scripts.analyze_news_with_llm import analyze_news_with_llm
                from scripts.generate_daily_news_digest import generate_digest

                for message in analyze_news_with_llm(loader.db_path, args.llm_config, target_date=state["latest_trade_date"], limit=args.llm_limit):
                    print(f"news_llm: {message}")
                for message in generate_digest(loader.db_path, args.llm_config, target_date=state["latest_trade_date"]):
                    print(f"news_digest: {message}")
            except Exception as exc:
                print(f"WARNING: LLM news step failed and daily market analysis remains complete: {exc}")
        if args.with_visuals:
            from src.visualization.dashboard_summary import build_visual_dashboard

            build_visual_dashboard(loader.db_path, target_date=state["latest_trade_date"])
    finally:
        store.close()
        try:
            loader.close()
        except Exception:
            pass


def _daily_report_markdown(state: dict, ranked: pd.DataFrame, db_path: Path) -> tuple[str, dict]:
    high_risk_count = int((ranked["risk_level"] == "HIGH").sum()) if "risk_level" in ranked else 0
    risk_text = ranked["risk_flags"].dropna().astype(str)
    main_risks = risk_text[risk_text != ""].str.split(",").explode().value_counts().head(5).to_dict()
    top20_cols = [col for col in ["rank", "ts_code", "name", "industry", "total_score", "risk_level"] if col in ranked.columns]
    lines = [
        "# Daily Analysis",
        "",
        f"- latest_trade_date: {state['latest_trade_date']}",
        f"- market_regime: {state['market_regime']}",
        f"- high_risk_count: {high_risk_count}",
        f"- main_risks: {main_risks}",
        "- data_range_start: 2025-01-01",
        f"- data_range_end: {state['latest_trade_date']}",
        f"- db_path: {db_path}",
        "",
        "## Top 20",
        "",
        ranked[top20_cols].head(20).to_markdown(index=False),
    ]
    news_lines = _news_report_lines(db_path, state["latest_trade_date"], ranked)
    if news_lines:
        lines.extend(["", "## News Explanation", "", *news_lines])
    recommendation_lines = _recommendation_report_lines(db_path, state["latest_trade_date"])
    if recommendation_lines:
        lines.extend(["", "## Recommendation Evaluation", "", *recommendation_lines])
    return "\n".join(lines) + "\n", main_risks


def _news_report_lines(db_path: Path, trade_date: str, ranked: pd.DataFrame) -> list[str]:
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            summary = conn.execute(
                """
                SELECT news_count, top_tags_json, top_industries_json, risk_news_count, market_summary, industry_summary
                FROM v_daily_news_summary
                WHERE trade_date <= ?
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                [trade_date],
            ).df()
            if summary.empty:
                return []
            row = summary.iloc[0]
            lines = [
                f"- 今日新闻数量：{int(row.get('news_count') or 0)}",
                f"- 热点主题：{_names_from_json(row.get('top_tags_json'))}",
                f"- 热点行业：{_names_from_json(row.get('top_industries_json'))}",
                f"- 风险新闻数量：{int(row.get('risk_news_count') or 0)}",
                f"- 新闻摘要：{row.get('market_summary', '')}",
                f"- 行业摘要：{row.get('industry_summary', '')}",
            ]
            if "ts_code" in ranked.columns:
                top_codes = ranked.head(20)["ts_code"].dropna().astype(str).tolist()
                if top_codes:
                    placeholders = ", ".join(["?"] * len(top_codes))
                    linked = conn.execute(
                        f"""
                        SELECT ts_code, name, COUNT(DISTINCT news_id) AS weak_news_count
                        FROM v_news_stock_link
                        WHERE ts_code IN ({placeholders})
                        GROUP BY ts_code, name
                        ORDER BY weak_news_count DESC, ts_code
                        LIMIT 10
                        """,
                        top_codes,
                    ).df()
                    if not linked.empty:
                        pairs = [f"{r.ts_code} {r.name}({int(r.weak_news_count)})" for r in linked.itertuples(index=False)]
                        lines.append(f"- Top20 弱关联新闻：{'、'.join(pairs)}")
            return lines
        finally:
            conn.close()
    except Exception:
        return []


def _names_from_json(value) -> str:
    try:
        items = json.loads(value or "[]")
    except Exception:
        items = []
    text = "、".join(f"{item.get('name')}({item.get('count')})" for item in items[:5]) if items else ""
    return text or "暂无"


def _recommendation_report_lines(db_path: Path, trade_date: str) -> list[str]:
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rolling = conn.execute(
                """
                SELECT *
                FROM v_recommendation_rolling_return_daily
                WHERE trade_date <= ?
                ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name
                LIMIT 6
                """,
                [trade_date],
            ).df()
            stability = conn.execute(
                """
                SELECT *
                FROM v_recommendation_stability_daily
                WHERE trade_date <= ?
                ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name
                LIMIT 6
                """,
                [trade_date],
            ).df()
            portfolio = conn.execute(
                """
                SELECT *
                FROM v_recommendation_live_portfolio_daily
                WHERE trade_date <= ?
                ORDER BY trade_date DESC, run_mode, recommendation_version, selector_name
                LIMIT 6
                """,
                [trade_date],
            ).df()
        finally:
            conn.close()
    except Exception:
        return []
    lines: list[str] = []
    if not rolling.empty:
        for row in rolling.to_dict("records"):
            annual_status = row.get("annual_return_status")
            annual_text = _pct(row.get("rolling_annual_net_return"))
            if annual_status == "INSUFFICIENT_HISTORY":
                annual_text = f"N/A（当前仅有 {int(row.get('annual_observation_count') or 0)} 个有效交易日，需要252个）"
            lines.extend([
                f"- run_mode: {str(row.get('run_mode', '')).upper()} version: {row.get('recommendation_version', '')} selector: {row.get('selector_name', '')}",
                f"  - 最近20个交易日滚动月度净收益: {_pct(row.get('rolling_monthly_net_return'))}，基准: {_pct(row.get('rolling_monthly_benchmark_return'))}，超额: {_pct(row.get('rolling_monthly_excess_return'))}，区间: {row.get('monthly_window_start')} 至 {row.get('monthly_window_end')}，样本: {int(row.get('monthly_observation_count') or 0)}，状态: {row.get('monthly_return_status')}",
                f"  - 最近252个交易日滚动年度净收益: {annual_text}，基准: {_pct(row.get('rolling_annual_benchmark_return'))}，超额: {_pct(row.get('rolling_annual_excess_return'))}，区间年化估算: {_pct(row.get('annualized_return_to_date'))}，区间: {row.get('annual_window_start')} 至 {row.get('annual_window_end')}，样本: {int(row.get('annual_observation_count') or 0)}，状态: {annual_status}",
            ])
    if not portfolio.empty:
        latest = portfolio.iloc[0]
        lines.append(f"- 当前滚动影子组合累计收益: {_pct(latest.get('cumulative_return'))} ({str(latest.get('run_mode', '')).upper()})")
    if not stability.empty:
        row = stability.iloc[0]
        lines.append(
            "- 推荐名单变动: "
            + f"目标推荐数={int(row.get('target_top_n') or row.get('top_n') or 0)}, "
            + f"实际推荐数={int(row.get('selected_count_current') or 0)}, "
            + f"填充率={_pct(row.get('fill_ratio'))}, "
            + f"保留股票={int(row.get('overlap_count') or 0)}, 新增股票={int(row.get('new_entry_count') or 0)}, 退出股票={int(row.get('dropout_count') or 0)}, "
            + f"名单重叠率={_pct(row.get('overlap_ratio_current'))}, 新增率={_pct(row.get('new_entry_ratio'))}, 退出率={_pct(row.get('dropout_ratio'))}, 权重换手率={_pct(row.get('weight_turnover_1d'))}"
        )
        if bool(row.get("is_first_observation")):
            lines.append("- 首次生成，无上一期名单可比较。")
    return lines


def _pct(value) -> str:
    try:
        if value is None or pd.isna(value):
            return "N/A"
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def _run_recommendation_evaluation(db_path: Path, config_path: str, target_date: str) -> None:
    steps = []
    try:
        from src.recommendation.tracker import update_tracking
        from src.recommendation.evaluator import evaluate_system, finalize_results
        from src.recommendation.stability import evaluate_recommendation_stability
        from src.visualization.recommendation_charts import build_recommendation_charts

        steps = [
            ("recommendation_tracking", lambda: update_tracking(db_path, config_path, target_date=target_date)),
            ("recommendation_finalize", lambda: finalize_results(db_path, config_path, end=target_date)),
            ("recommendation_stability", lambda: evaluate_recommendation_stability(db_path, config_path, target_date=target_date)),
            ("recommendation_summary", lambda: evaluate_system(db_path, config_path, target_date=target_date)),
            ("recommendation_charts", lambda: [f"chart_snapshots: {len(build_recommendation_charts(db_path, target_date).get('snapshots', []))}"]),
        ]
        for name, func in steps:
            try:
                for line in func():
                    print(f"{name}: {line}")
            except Exception as exc:
                print(f"WARNING: {name} failed and daily market analysis will continue: {exc}")
    except Exception as exc:
        print(f"WARNING: recommendation evaluation setup failed and daily market analysis will continue: {exc}")


if __name__ == "__main__":
    main()
