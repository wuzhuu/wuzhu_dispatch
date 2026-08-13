from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.analysis.path_resolver import resolve_data_root
from src.utils.config import load_settings, resolve_db_path


DATE_DATASETS = {
    "factor_daily": ["year", "month"],
    "score_daily": ["year", "month"],
    "risk_flag_daily": ["year", "month"],
    "market_state_daily": ["year", "month"],
    "analysis_universe": ["year", "month"],
    "ranking_change_daily": ["year", "month"],
    "stock_report": ["ts_code", "year", "month"],
    "daily_analysis_report": ["year", "month"],
    "analysis_validation": ["year", "month"],
    "database_inspection": ["year", "month"],
    "database_repair_log": ["year", "month"],
    "data_quality_price": ["year", "month"],
    "backtest_validation": ["year", "month"],
    "visualization_validation": ["year", "month"],
    "maintenance_log": ["year", "month"],
    "visualization_snapshot": ["year", "month"],
    "visualization_metric": ["year", "month"],
    "news_clean": ["year", "month"],
    "news_tag_daily": ["year", "month"],
    "news_industry_link": ["year", "month"],
    "news_stock_link": ["year", "month"],
    "daily_news_summary": ["year", "month"],
    "news_factor_daily": ["year", "month"],
    "news_llm_analysis": ["year", "month"],
    "llm_usage_log": ["year", "month"],
    "news_llm_stock_link": ["year", "month"],
    "news_llm_industry_link": ["year", "month"],
    "news_fused_analysis": ["year", "month"],
    "daily_news_llm_digest": ["year", "month"],
    "recommendation_batch": ["year", "month"],
    "recommendation_item": ["year", "month"],
    "recommendation_universe": ["year", "month"],
    "recommendation_tracking_daily": ["year", "month"],
    "recommendation_item_result": ["year", "month"],
    "recommendation_batch_result": ["year", "month"],
    "recommendation_live_portfolio_daily": ["year", "month"],
    "recommendation_rolling_return_daily": ["year", "month"],
    "recommendation_evaluation_daily": ["year", "month"],
    "recommendation_explanation": ["year", "month"],
    "recommendation_stability_daily": ["year", "month"],
    "recommendation_list_change_detail": ["year", "month"],
    "recommendation_repair_log": ["year", "month"],
}

RUN_DATASETS = {
    "backtest_result": ["strategy_name", "run_id"],
    "backtest_monthly_returns": ["strategy_name", "run_id"],
    "backtest_holdings": ["strategy_name", "run_id"],
}

DATASET_KEYS = {
    "factor_daily": ["ts_code", "trade_date"],
    "score_daily": ["ts_code", "trade_date"],
    "risk_flag_daily": ["ts_code", "trade_date"],
    "market_state_daily": ["trade_date"],
    "analysis_universe": ["ts_code", "trade_date", "universe_name"],
    "ranking_change_daily": ["ts_code", "trade_date", "top_n"],
    "stock_report": ["ts_code", "trade_date", "report_type", "report_version"],
    "daily_analysis_report": ["trade_date", "report_type", "report_version"],
    "analysis_validation": ["trade_date", "check_name"],
    "database_inspection": ["trade_date", "check_name"],
    "database_repair_log": ["trade_date", "check_name"],
    "data_quality_price": ["trade_date", "check_name"],
    "backtest_validation": ["trade_date", "check_name"],
    "visualization_validation": ["trade_date", "check_name"],
    "maintenance_log": ["trade_date", "check_name"],
    "backtest_result": ["run_id"],
    "backtest_monthly_returns": ["run_id", "period"],
    "backtest_holdings": ["run_id", "rebalance_date", "ts_code"],
    "visualization_snapshot": ["chart_id"],
    "visualization_metric": ["metric_name", "trade_date", "group_name", "metric_version"],
    "news_clean": ["news_id"],
    "news_tag_daily": ["news_id", "tag_type", "tag_value"],
    "news_industry_link": ["news_id", "industry_code", "industry_name", "match_keyword"],
    "news_stock_link": ["news_id", "ts_code", "match_type"],
    "daily_news_summary": ["trade_date"],
    "news_factor_daily": ["trade_date", "industry_code", "industry_name"],
    "news_llm_analysis": ["news_id", "model_name", "prompt_version", "analysis_version"],
    "llm_usage_log": ["request_id"],
    "news_llm_stock_link": ["news_id", "ts_code", "model_name", "analysis_version"],
    "news_llm_industry_link": ["news_id", "industry_code", "model_name", "analysis_version"],
    "news_fused_analysis": ["news_id", "fusion_version"],
    "daily_news_llm_digest": ["trade_date", "model_name", "prompt_version", "digest_version"],
    "recommendation_batch": ["signal_date", "recommendation_version", "run_mode"],
    "recommendation_item": ["batch_id", "ts_code"],
    "recommendation_universe": ["batch_id", "ts_code"],
    "recommendation_tracking_daily": ["batch_id", "ts_code", "tracking_date"],
    "recommendation_item_result": ["batch_id", "ts_code"],
    "recommendation_batch_result": ["batch_id"],
    "recommendation_live_portfolio_daily": ["trading_date", "run_mode", "recommendation_version", "selector_name"],
    "recommendation_rolling_return_daily": ["trading_date", "run_mode", "recommendation_version", "selector_name"],
    "recommendation_evaluation_daily": ["evaluation_date", "recommendation_version"],
    "recommendation_explanation": ["batch_id", "ts_code", "explanation_version"],
    "recommendation_stability_daily": ["trade_date", "recommendation_version", "selector_name", "run_mode", "top_n"],
    "recommendation_list_change_detail": ["trade_date", "recommendation_version", "selector_name", "run_mode", "ts_code", "change_type"],
    "recommendation_repair_log": ["trade_date", "check_name"],
}

EMPTY_VIEW_SCHEMAS = {
    "news_clean": [
        ("news_id", "VARCHAR"),
        ("published_at", "TIMESTAMP"),
        ("trade_date", "DATE"),
        ("source", "VARCHAR"),
        ("source_type", "VARCHAR"),
        ("title", "VARCHAR"),
        ("summary", "VARCHAR"),
        ("url", "VARCHAR"),
        ("language", "VARCHAR"),
        ("title_norm", "VARCHAR"),
        ("summary_norm", "VARCHAR"),
        ("duplicate_key", "VARCHAR"),
        ("is_duplicate", "BOOLEAN"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_tag_daily": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("tag_type", "VARCHAR"),
        ("tag_value", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("method", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_industry_link": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("industry_code", "VARCHAR"),
        ("industry_name", "VARCHAR"),
        ("match_keyword", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("method", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_stock_link": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("ts_code", "VARCHAR"),
        ("name", "VARCHAR"),
        ("match_type", "VARCHAR"),
        ("match_keyword", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("method", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "daily_news_summary": [
        ("trade_date", "DATE"),
        ("source_count", "BIGINT"),
        ("news_count", "BIGINT"),
        ("top_tags_json", "VARCHAR"),
        ("top_industries_json", "VARCHAR"),
        ("risk_news_count", "BIGINT"),
        ("market_summary", "VARCHAR"),
        ("industry_summary", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_factor_daily": [
        ("trade_date", "DATE"),
        ("industry_code", "VARCHAR"),
        ("industry_name", "VARCHAR"),
        ("news_count", "BIGINT"),
        ("risk_news_count", "BIGINT"),
        ("news_heat_score", "DOUBLE"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_llm_analysis": [
        ("analysis_id", "VARCHAR"),
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("provider", "VARCHAR"),
        ("protocol", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("prompt_version", "VARCHAR"),
        ("analysis_version", "VARCHAR"),
        ("is_financial_news", "BOOLEAN"),
        ("event_type", "VARCHAR"),
        ("sentiment", "VARCHAR"),
        ("sentiment_score", "DOUBLE"),
        ("impact_direction", "VARCHAR"),
        ("impact_horizon", "VARCHAR"),
        ("importance_score", "BIGINT"),
        ("market_relevance", "BIGINT"),
        ("risk_level", "VARCHAR"),
        ("topics_json", "VARCHAR"),
        ("industries_json", "VARCHAR"),
        ("stocks_json", "VARCHAR"),
        ("organizations_json", "VARCHAR"),
        ("countries_regions_json", "VARCHAR"),
        ("summary", "VARCHAR"),
        ("key_points_json", "VARCHAR"),
        ("evidence_json", "VARCHAR"),
        ("uncertainty", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("rule_tags_json", "VARCHAR"),
        ("raw_response", "VARCHAR"),
        ("status", "VARCHAR"),
        ("error_type", "VARCHAR"),
        ("error_message", "VARCHAR"),
        ("input_tokens", "BIGINT"),
        ("output_tokens", "BIGINT"),
        ("latency_ms", "BIGINT"),
        ("estimated_cost", "DOUBLE"),
        ("created_at", "TIMESTAMP"),
    ],
    "llm_usage_log": [
        ("request_id", "VARCHAR"),
        ("task_type", "VARCHAR"),
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("provider", "VARCHAR"),
        ("protocol", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("prompt_version", "VARCHAR"),
        ("status", "VARCHAR"),
        ("input_tokens", "BIGINT"),
        ("output_tokens", "BIGINT"),
        ("latency_ms", "BIGINT"),
        ("retry_count", "BIGINT"),
        ("estimated_cost", "DOUBLE"),
        ("error_type", "VARCHAR"),
        ("error_message", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_llm_stock_link": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("ts_code", "VARCHAR"),
        ("stock_name", "VARCHAR"),
        ("relation", "VARCHAR"),
        ("llm_confidence", "DOUBLE"),
        ("entity_match_confidence", "DOUBLE"),
        ("final_confidence", "DOUBLE"),
        ("evidence", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("analysis_version", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_llm_industry_link": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("industry_code", "VARCHAR"),
        ("industry_name", "VARCHAR"),
        ("relation", "VARCHAR"),
        ("llm_confidence", "DOUBLE"),
        ("entity_match_confidence", "DOUBLE"),
        ("final_confidence", "DOUBLE"),
        ("evidence", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("analysis_version", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "news_fused_analysis": [
        ("news_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("rule_tags_json", "VARCHAR"),
        ("llm_topics_json", "VARCHAR"),
        ("final_topics_json", "VARCHAR"),
        ("final_sentiment", "VARCHAR"),
        ("final_sentiment_score", "DOUBLE"),
        ("final_risk_level", "VARCHAR"),
        ("final_importance_score", "BIGINT"),
        ("final_market_relevance", "BIGINT"),
        ("final_industries_json", "VARCHAR"),
        ("final_stocks_json", "VARCHAR"),
        ("conflict_flags_json", "VARCHAR"),
        ("fusion_version", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
    ],
    "daily_news_llm_digest": [
        ("digest_id", "VARCHAR"),
        ("trade_date", "DATE"),
        ("provider", "VARCHAR"),
        ("model_name", "VARCHAR"),
        ("prompt_version", "VARCHAR"),
        ("digest_version", "VARCHAR"),
        ("source_news_count", "BIGINT"),
        ("analyzed_news_count", "BIGINT"),
        ("failed_news_count", "BIGINT"),
        ("market_regime", "VARCHAR"),
        ("top_topics_json", "VARCHAR"),
        ("top_industries_json", "VARCHAR"),
        ("top_stocks_json", "VARCHAR"),
        ("top_events_json", "VARCHAR"),
        ("risk_events_json", "VARCHAR"),
        ("market_summary", "VARCHAR"),
        ("macro_summary", "VARCHAR"),
        ("industry_summary", "VARCHAR"),
        ("stock_summary", "VARCHAR"),
        ("risk_summary", "VARCHAR"),
        ("uncertainty_summary", "VARCHAR"),
        ("report_markdown", "VARCHAR"),
        ("raw_response", "VARCHAR"),
        ("status", "VARCHAR"),
        ("input_tokens", "BIGINT"),
        ("output_tokens", "BIGINT"),
        ("latency_ms", "BIGINT"),
        ("created_at", "TIMESTAMP"),
    ],
    "recommendation_batch": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"),
        ("run_mode", "VARCHAR"),
        ("factor_version", "VARCHAR"), ("score_version", "VARCHAR"), ("risk_version", "VARCHAR"), ("news_analysis_version", "VARCHAR"),
        ("top_n", "BIGINT"), ("target_top_n", "BIGINT"), ("holding_period_days", "BIGINT"), ("entry_price_type", "VARCHAR"), ("weighting_method", "VARCHAR"),
        ("benchmark_index", "VARCHAR"), ("universe_count", "BIGINT"), ("eligible_count", "BIGINT"), ("selected_count", "BIGINT"),
        ("fill_ratio", "DOUBLE"), ("shortfall_reason_json", "VARCHAR"), ("quality_status", "VARCHAR"),
        ("market_regime", "VARCHAR"), ("config_json", "VARCHAR"), ("status", "VARCHAR"), ("created_at", "TIMESTAMP"), ("updated_at", "TIMESTAMP"),
    ],
    "recommendation_item": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("run_mode", "VARCHAR"), ("ts_code", "VARCHAR"), ("name", "VARCHAR"),
        ("industry_code", "VARCHAR"), ("industry_name", "VARCHAR"), ("rank", "BIGINT"), ("final_score", "DOUBLE"), ("total_score", "DOUBLE"),
        ("risk_level", "VARCHAR"), ("risk_flags", "VARCHAR"), ("industry_strength_score", "DOUBLE"), ("news_support_score", "DOUBLE"),
        ("industry_data_source", "VARCHAR"), ("industry_missing", "BOOLEAN"), ("industry_warning", "VARCHAR"),
        ("selection_reason_json", "VARCHAR"), ("risk_summary_json", "VARCHAR"), ("weight", "DOUBLE"), ("entry_date", "DATE"),
        ("entry_price", "DOUBLE"), ("tracking_status", "VARCHAR"), ("created_at", "TIMESTAMP"), ("updated_at", "TIMESTAMP"),
    ],
    "recommendation_universe": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("ts_code", "VARCHAR"), ("included", "BOOLEAN"),
        ("exclude_reason", "VARCHAR"), ("history_days", "BIGINT"), ("amount_ma20", "DOUBLE"), ("risk_level", "VARCHAR"),
        ("is_st", "BOOLEAN"), ("is_suspended", "BOOLEAN"), ("listing_days", "BIGINT"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_tracking_daily": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("run_mode", "VARCHAR"), ("ts_code", "VARCHAR"), ("tracking_date", "DATE"),
        ("trading_day_no", "BIGINT"), ("entry_date", "DATE"), ("entry_price", "DOUBLE"), ("close", "DOUBLE"), ("daily_return", "DOUBLE"),
        ("cumulative_return", "DOUBLE"), ("net_cumulative_return", "DOUBLE"), ("benchmark_cumulative_return", "DOUBLE"), ("excess_return", "DOUBLE"),
        ("industry_benchmark_return", "DOUBLE"), ("industry_excess_return", "DOUBLE"), ("running_max_return", "DOUBLE"),
        ("running_min_return", "DOUBLE"), ("drawdown_from_peak", "DOUBLE"), ("max_drawdown_to_date", "DOUBLE"), ("volume", "DOUBLE"),
        ("amount", "DOUBLE"), ("data_status", "VARCHAR"), ("return_1d", "DOUBLE"), ("return_5d", "DOUBLE"), ("return_10d", "DOUBLE"),
        ("return_20d", "DOUBLE"), ("return_30d", "DOUBLE"), ("created_at", "TIMESTAMP"), ("updated_at", "TIMESTAMP"),
    ],
    "recommendation_item_result": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("ts_code", "VARCHAR"), ("name", "VARCHAR"),
        ("entry_date", "DATE"), ("entry_price", "DOUBLE"), ("exit_date", "DATE"), ("exit_price", "DOUBLE"), ("actual_trading_days", "BIGINT"),
        ("gross_return_30d", "DOUBLE"), ("net_return_30d", "DOUBLE"), ("current_gross_return", "DOUBLE"), ("current_net_return", "DOUBLE"),
        ("final_gross_return_30d", "DOUBLE"), ("final_net_return_30d", "DOUBLE"), ("benchmark_return_30d", "DOUBLE"), ("excess_return_30d", "DOUBLE"),
        ("industry_return_30d", "DOUBLE"), ("industry_excess_return_30d", "DOUBLE"), ("max_gain_30d", "DOUBLE"), ("max_drawdown_30d", "DOUBLE"),
        ("positive_day_count", "BIGINT"), ("negative_day_count", "BIGINT"), ("first_positive_day", "BIGINT"), ("hit_positive_return", "BOOLEAN"),
        ("weight", "DOUBLE"),
        ("position_notional", "DOUBLE"), ("buy_commission", "DOUBLE"), ("sell_commission", "DOUBLE"), ("buy_cost_rate", "DOUBLE"),
        ("sell_cost_rate", "DOUBLE"), ("stamp_duty_cost_rate", "DOUBLE"), ("slippage_cost_rate", "DOUBLE"), ("total_cost_rate", "DOUBLE"),
        ("cost_warning", "VARCHAR"),
        ("outperform_benchmark", "BOOLEAN"), ("outcome_status", "VARCHAR"), ("completed_at", "TIMESTAMP"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_batch_result": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("signal_date", "DATE"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"), ("selected_count", "BIGINT"),
        ("completed_count", "BIGINT"), ("positive_count", "BIGINT"), ("negative_count", "BIGINT"), ("hit_rate", "DOUBLE"),
        ("equal_weight_gross_return", "DOUBLE"), ("equal_weight_net_return", "DOUBLE"), ("weighted_gross_return", "DOUBLE"), ("weighted_net_return", "DOUBLE"),
        ("simple_mean_gross_return", "DOUBLE"), ("simple_mean_net_return", "DOUBLE"), ("weighting_method", "VARCHAR"), ("weight_sum", "DOUBLE"),
        ("benchmark_return", "DOUBLE"), ("excess_return", "DOUBLE"),
        ("industry_adjusted_return", "DOUBLE"), ("median_stock_return", "DOUBLE"), ("best_stock_return", "DOUBLE"), ("worst_stock_return", "DOUBLE"),
        ("portfolio_max_drawdown", "DOUBLE"), ("annualized_volatility", "DOUBLE"), ("sharpe", "DOUBLE"), ("concentration_hhi", "DOUBLE"), ("transaction_cost", "DOUBLE"),
        ("completed_at", "TIMESTAMP"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_live_portfolio_daily": [
        ("trading_date", "DATE"), ("trade_date", "DATE"), ("run_mode", "VARCHAR"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"),
        ("active_batch_count", "BIGINT"), ("active_position_count", "BIGINT"),
        ("gross_daily_return", "DOUBLE"), ("net_daily_return", "DOUBLE"), ("gross_equity", "DOUBLE"), ("net_equity", "DOUBLE"),
        ("cumulative_return", "DOUBLE"), ("benchmark_cumulative_return", "DOUBLE"),
        ("excess_cumulative_return", "DOUBLE"), ("turnover", "DOUBLE"), ("transaction_cost", "DOUBLE"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_rolling_return_daily": [
        ("trade_date", "DATE"), ("trading_date", "DATE"), ("run_mode", "VARCHAR"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"),
        ("rolling_monthly_gross_return", "DOUBLE"), ("rolling_monthly_net_return", "DOUBLE"), ("rolling_monthly_benchmark_return", "DOUBLE"),
        ("rolling_monthly_excess_return", "DOUBLE"), ("monthly_window_start", "DATE"), ("monthly_window_end", "DATE"),
        ("monthly_observation_count", "BIGINT"), ("monthly_return_status", "VARCHAR"),
        ("rolling_annual_gross_return", "DOUBLE"), ("rolling_annual_net_return", "DOUBLE"), ("rolling_annual_benchmark_return", "DOUBLE"),
        ("rolling_annual_excess_return", "DOUBLE"), ("annualized_return_to_date", "DOUBLE"), ("annual_window_start", "DATE"),
        ("annual_window_end", "DATE"), ("annual_observation_count", "BIGINT"), ("annual_return_status", "VARCHAR"),
        ("quality_status", "VARCHAR"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_evaluation_daily": [
        ("evaluation_date", "DATE"), ("trade_date", "DATE"), ("recommendation_version", "VARCHAR"), ("completed_batch_count", "BIGINT"),
        ("completed_stock_count", "BIGINT"), ("average_return_30d", "DOUBLE"), ("median_return_30d", "DOUBLE"), ("weighted_return_30d", "DOUBLE"),
        ("positive_hit_rate", "DOUBLE"), ("benchmark_outperform_rate", "DOUBLE"), ("average_excess_return", "DOUBLE"), ("average_max_drawdown", "DOUBLE"),
        ("profit_loss_ratio", "DOUBLE"), ("return_std", "DOUBLE"), ("information_ratio", "DOUBLE"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_explanation": [
        ("batch_id", "VARCHAR"), ("trade_date", "DATE"), ("ts_code", "VARCHAR"), ("explanation_version", "VARCHAR"), ("model_name", "VARCHAR"),
        ("factor_summary", "VARCHAR"), ("industry_summary", "VARCHAR"), ("news_summary", "VARCHAR"), ("risk_summary", "VARCHAR"),
        ("uncertainty", "VARCHAR"), ("explanation_markdown", "VARCHAR"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_stability_daily": [
        ("trade_date", "DATE"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"), ("run_mode", "VARCHAR"), ("top_n", "BIGINT"), ("target_top_n", "BIGINT"),
        ("overlap_ratio_1d", "DOUBLE"), ("overlap_ratio_5d", "DOUBLE"), ("turnover_1d", "DOUBLE"),
        ("selected_count", "BIGINT"), ("fill_ratio", "DOUBLE"), ("selected_count_current", "BIGINT"), ("selected_count_previous", "BIGINT"), ("overlap_count", "BIGINT"),
        ("overlap_ratio_current", "DOUBLE"), ("jaccard_overlap", "DOUBLE"), ("new_entry_count", "BIGINT"),
        ("new_entry_ratio", "DOUBLE"), ("dropout_count", "BIGINT"), ("dropout_ratio", "DOUBLE"),
        ("weight_turnover_1d", "DOUBLE"), ("is_first_observation", "BOOLEAN"), ("mean_rank_change", "DOUBLE"),
        ("median_rank_change", "DOUBLE"), ("mean_consecutive_days", "DOUBLE"), ("median_consecutive_days", "DOUBLE"),
        ("industry_concentration_hhi", "DOUBLE"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_list_change_detail": [
        ("trade_date", "DATE"), ("recommendation_version", "VARCHAR"), ("selector_name", "VARCHAR"), ("run_mode", "VARCHAR"),
        ("ts_code", "VARCHAR"), ("name", "VARCHAR"), ("change_type", "VARCHAR"), ("current_rank", "BIGINT"), ("previous_rank", "BIGINT"),
        ("final_score", "DOUBLE"), ("industry_name", "VARCHAR"), ("change_reason", "VARCHAR"), ("created_at", "TIMESTAMP"),
    ],
    "recommendation_repair_log": [
        ("trade_date", "DATE"), ("check_name", "VARCHAR"), ("status", "VARCHAR"), ("message", "VARCHAR"),
        ("affected_rows", "BIGINT"), ("created_at", "TIMESTAMP"),
    ],
}


class AnalysisLakeStore:
    def __init__(self, db_path: str | Path | None = None, data_root: str | Path | None = None):
        settings = load_settings()
        self.db_path = Path(db_path).expanduser().resolve(strict=False) if db_path else resolve_db_path(settings)
        self.data_root = Path(data_root).expanduser().resolve(strict=False) if data_root else resolve_data_root(self.db_path)
        self.lake_root = self.data_root / "lake"
        self.backup_root = self.lake_root / "backups"
        self.conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lake_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        if self.conn is None:
            self.conn = duckdb.connect(str(self.db_path))
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def write_dataset(self, dataset: str, df: pd.DataFrame, keys: list[str] | None = None, skip_backup: bool = False) -> list[Path]:
        if df is None or df.empty:
            self.refresh_views([dataset])
            return []
        keys = keys or DATASET_KEYS.get(dataset, [])
        out = self._prepare(dataset, df)
        written: list[Path] = []
        for rel, part_df in self._partition_groups(dataset, out):
            merged = self._merge_partition(rel, part_df, keys)
            written.append(self._write_partition(rel, merged, skip_backup=skip_backup))
        self.refresh_views([dataset])
        return written

    def read_dataset(self, dataset: str) -> pd.DataFrame:
        root = self.lake_root / dataset
        if not root.exists() or not any(root.rglob("*.parquet")):
            return pd.DataFrame()
        glob = (root / "**" / "*.parquet").as_posix()
        return duckdb.connect().execute(
            "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=true)",
            [glob],
        ).df()

    def refresh_views(self, datasets: list[str] | None = None) -> None:
        names = datasets or sorted(set(DATE_DATASETS) | set(RUN_DATASETS))
        conn = self.connect()
        for dataset in names:
            view_name = f"v_{dataset}"
            root = self.lake_root / dataset
            if root.exists() and any(root.rglob("*.parquet")):
                glob = (root / "**" / "*.parquet").as_posix().replace("'", "''")
                base = f"read_parquet('{glob}', union_by_name=true, hive_partitioning=true)"
                sql = self._select_with_schema_defaults(dataset, base)
                keys = DATASET_KEYS.get(dataset, [])
                if dataset.startswith("news_") or dataset.startswith("recommendation_") or dataset in {"daily_news_summary", "daily_news_llm_digest", "llm_usage_log"}:
                    sql += (
                        " QUALIFY ROW_NUMBER() OVER (PARTITION BY "
                        + ", ".join(keys)
                        + " ORDER BY created_at DESC NULLS LAST) = 1"
                    )
                conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS {sql}")
            else:
                conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS {self._empty_select(dataset)}")

    def write_validation(self, rows: list[dict[str, Any]]) -> list[Path]:
        df = pd.DataFrame(rows)
        if df.empty:
            return []
        if "created_at" not in df.columns:
            df["created_at"] = pd.Timestamp.now()
        return self.write_dataset("analysis_validation", df)

    def _prepare(self, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        now = pd.Timestamp.now()
        if "created_at" not in out.columns:
            out["created_at"] = now
        if dataset in DATE_DATASETS:
            date_col = "trade_date" if "trade_date" in out.columns else "created_at"
            out["trade_date"] = pd.to_datetime(out[date_col], errors="coerce").dt.date
            out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
            out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        if dataset in RUN_DATASETS:
            if "run_id" not in out.columns:
                out["run_id"] = uuid.uuid4().hex
            if "strategy_name" not in out.columns:
                out["strategy_name"] = "default"
        for col in ("summary_json",):
            if col in out.columns:
                out[col] = out[col].map(lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v)
        return out

    def _partition_groups(self, dataset: str, df: pd.DataFrame):
        partition_cols = DATE_DATASETS.get(dataset) or RUN_DATASETS.get(dataset)
        if not partition_cols:
            yield Path(dataset), df
            return
        for values, part_df in df.groupby(partition_cols, dropna=False):
            if not isinstance(values, tuple):
                values = (values,)
            rel = Path(dataset)
            for col, value in zip(partition_cols, values):
                rel /= f"{col}={self._partition_value(value)}"
            yield rel, part_df.drop(columns=[c for c in ("year", "month") if c in part_df.columns])

    def _merge_partition(self, rel: Path, part_df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        existing = self._read_partition(rel)
        merged = pd.concat([existing, part_df], ignore_index=True) if not existing.empty else part_df.copy()
        if keys and all(key in merged.columns for key in keys):
            # Normalize date/timestamp columns to avoid pandas sort comparison errors
            for col in merged.columns:
                if pd.api.types.is_object_dtype(merged[col]):
                    try:
                        sample = merged[col].dropna().iloc[0] if not merged[col].dropna().empty else None
                        if isinstance(sample, (pd.Timestamp, datetime, date)):
                            merged[col] = pd.to_datetime(merged[col], errors="coerce")
                    except (IndexError, TypeError):
                        pass
            sort_cols = keys + [col for col in ("created_at", "updated_at") if col in merged.columns]
            ascending = [True] * len(keys) + [False] * (len(sort_cols) - len(keys))
            merged = merged.sort_values(sort_cols, ascending=ascending).drop_duplicates(keys, keep="first")
        return merged.reset_index(drop=True)

    def _read_partition(self, rel: Path) -> pd.DataFrame:
        files = sorted((self.lake_root / rel).glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)

    def _write_partition(self, rel: Path, df: pd.DataFrame, skip_backup: bool = False) -> Path:
        partition_dir = self.lake_root / rel
        if partition_dir.exists() and not skip_backup and not self._backup_disabled():
            # 硬链接快照 + 保留策略，防止 backups/ 无限膨胀
            dest = self.backup_root / f"{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(partition_dir, dest, copy_function=os.link)
            self._prune_backups()
        if partition_dir.exists():
            shutil.rmtree(partition_dir)
        partition_dir.mkdir(parents=True, exist_ok=True)
        path = partition_dir / f"part-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}.parquet"
        df.to_parquet(path, index=False)
        return path

    @staticmethod
    def _backup_disabled() -> bool:
        return int(os.environ.get("LAKE_BACKUP_KEEP", "1")) <= 0

    def _prune_backups(self, keep: int | None = None) -> None:
        """备份保留策略：每个分区只保留最近 N 份快照（默认 1 份），防止 backups/ 膨胀。

        用 LAKE_BACKUP_KEEP=0 可完全关闭备份（_write_partition 直接跳过）。
        """
        if keep is None:
            keep = int(os.environ.get("LAKE_BACKUP_KEEP", "1"))
        if keep <= 0 or not self.backup_root.exists():
            return
        groups: dict[str, list[str]] = {}
        for name in sorted(os.listdir(self.backup_root)):
            bdir = self.backup_root / name
            if not bdir.is_dir() or "-" not in name:
                continue
            for p in bdir.rglob("*.parquet"):
                rel = str(p.relative_to(bdir).parent)
                lst = groups.setdefault(rel, [])
                if name not in lst:
                    lst.append(name)
        for rel, names in groups.items():
            for old in names[:-keep]:
                shutil.rmtree(self.backup_root / old, ignore_errors=True)

    @staticmethod
    def _partition_value(value: Any) -> str:
        text = "unknown" if pd.isna(value) else str(value)
        return text.replace("/", "_").replace("\\", "_").replace(" ", "_")

    @staticmethod
    def _empty_select(dataset: str) -> str:
        columns = EMPTY_VIEW_SCHEMAS.get(dataset)
        if not columns:
            return "SELECT NULL AS empty WHERE FALSE"
        select_cols = ", ".join(f"CAST(NULL AS {sql_type}) AS {name}" for name, sql_type in columns)
        return f"SELECT {select_cols} WHERE FALSE"

    def _select_with_schema_defaults(self, dataset: str, base: str) -> str:
        columns = EMPTY_VIEW_SCHEMAS.get(dataset, [])
        if not columns:
            return f"SELECT * FROM {base}"
        try:
            present = set(self.connect().execute(f"DESCRIBE SELECT * FROM {base}").df()["column_name"].astype(str))
        except Exception:
            return f"SELECT * FROM {base}"
        extras = []
        for name, sql_type in columns:
            if name in present:
                continue
            default = "'live_shadow'" if name == "run_mode" else ("''" if sql_type == "VARCHAR" else "NULL")
            extras.append(f"CAST({default} AS {sql_type}) AS {name}")
        if not extras:
            return f"SELECT * FROM {base}"
        return f"SELECT *, {', '.join(extras)} FROM {base}"
