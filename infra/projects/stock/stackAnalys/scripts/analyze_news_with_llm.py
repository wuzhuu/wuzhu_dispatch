from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_news_analysis import build_news_analysis
from src.analysis.news_entity_resolver import NewsEntityResolver
from src.analysis.news_fusion import build_fused_analysis
from src.analysis.news_lake_writer import append_dataset, refresh_news_llm_views
from src.llm.client import LLMClient
from src.llm.config import load_llm_config
from src.llm.exceptions import LLMError, LLMErrorType
from src.llm.prompts import NEWS_EXTRACTION_SYSTEM_PROMPT, news_extraction_user_prompt
from src.llm.schemas import schema_json, validate_news_analysis
from src.utils.config import add_db_path_arg, resolve_db_path


PROMPT_VERSION = "news_extraction_v1"
ANALYSIS_VERSION = "llm_analysis_v1"
FUSION_VERSION = "fusion_v1"


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze cleaned RSS news with a configurable LLM provider.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="../key/stackAnalys_llm.yaml")
    parser.add_argument("--profile", default="extraction")
    parser.add_argument("--date")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--news-id")
    parser.add_argument("--only-failed", action="store_true")
    args = parser.parse_args()
    db_path = resolve_db_path(cli_db_path=args.db_path)
    result = analyze_news_with_llm(
        db_path=db_path,
        config_path=args.config,
        profile_name=args.profile,
        target_date=args.date,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        news_id=args.news_id,
        only_failed=args.only_failed,
    )
    for line in result:
        print(line)


def analyze_news_with_llm(
    db_path: str | Path,
    config_path: str | Path,
    profile_name: str = "extraction",
    target_date: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    news_id: str | None = None,
    only_failed: bool = False,
) -> list[str]:
    db_path = Path(db_path).expanduser().resolve(strict=False)
    lines = []
    build_news_analysis(db_path, target_date=target_date)
    try:
        cfg = load_llm_config(config_path)
    except FileNotFoundError as exc:
        refresh_news_llm_views(db_path)
        return [f"WARNING: {exc}; skipped LLM and kept keyword-rule outputs."]
    profile = getattr(cfg, profile_name)
    lines.append(f"provider: {profile.provider}")
    lines.append(f"protocol: {profile.protocol}")
    lines.append(f"model: {profile.model}")
    if not cfg.enabled:
        refresh_news_llm_views(db_path)
        return lines + ["WARNING: llm.enabled=false; skipped LLM and kept keyword-rule outputs."]
    if not profile.api_key and not profile.mock:
        refresh_news_llm_views(db_path)
        return lines + [f"WARNING: missing API key from {profile.api_key_source_label}; skipped LLM and kept keyword-rule outputs."]

    conn = duckdb.connect(str(db_path))
    try:
        candidates = _load_candidates(conn, cfg.news_llm, target_date, limit, news_id, only_failed, force, profile.model)
        lines.append(f"candidate_news: {len(candidates)}")
        if dry_run:
            return lines + ["dry_run: true", "called_provider: false"]
        client = LLMClient(profile)
        resolver = NewsEntityResolver(conn)
        analysis_rows: list[dict[str, Any]] = []
        usage_rows: list[dict[str, Any]] = []
        stock_rows: list[dict[str, Any]] = []
        industry_rows: list[dict[str, Any]] = []
        success = failed = skipped = 0
        for item in candidates.itertuples(index=False):
            existing = _existing_success(conn, item.news_id, profile.model)
            if existing and not force:
                skipped += 1
                continue
            row, usage = _analyze_one(client, profile, item)
            analysis_rows.append(row)
            usage_rows.append(usage)
            if row["status"] == "SUCCESS":
                success += 1
                stocks = json.loads(row["stocks_json"])
                industries = json.loads(row["industries_json"])
                stock_rows.extend(resolver.stock_links(item.news_id, item.trade_date, stocks, profile.model, ANALYSIS_VERSION))
                industry_rows.extend(resolver.industry_links(item.news_id, item.trade_date, industries, profile.model, ANALYSIS_VERSION))
            else:
                failed += 1
        append_dataset(db_path, "news_llm_analysis", pd.DataFrame(analysis_rows))
        append_dataset(db_path, "llm_usage_log", pd.DataFrame(usage_rows))
        append_dataset(db_path, "news_llm_stock_link", pd.DataFrame(stock_rows))
        append_dataset(db_path, "news_llm_industry_link", pd.DataFrame(industry_rows))
        fused = build_fused_analysis(
            _safe_df(conn, "v_news_clean"),
            _safe_df(conn, "v_news_tag_daily"),
            _safe_df(conn, "v_news_llm_analysis"),
            _safe_df(conn, "v_news_llm_industry_link"),
            _safe_df(conn, "v_news_llm_stock_link"),
            FUSION_VERSION,
        )
        append_dataset(db_path, "news_fused_analysis", fused)
        refresh_news_llm_views(db_path)
        lines.extend(
            [
                f"success_count: {success}",
                f"failed_count: {failed}",
                f"skipped_count: {skipped}",
                f"news_llm_analysis_rows_written: {len(analysis_rows)}",
                f"news_llm_stock_link_rows_written: {len(stock_rows)}",
                f"news_llm_industry_link_rows_written: {len(industry_rows)}",
                f"news_fused_analysis_rows_written: {len(fused)}",
            ]
        )
        return lines
    finally:
        conn.close()


def _load_candidates(
    conn: duckdb.DuckDBPyConnection,
    options: dict[str, Any],
    target_date: str | None,
    limit: int | None,
    news_id: str | None,
    only_failed: bool,
    force: bool,
    model_name: str,
) -> pd.DataFrame:
    if not _relation_exists(conn, "v_news_clean"):
        return pd.DataFrame()
    where = ["1=1"]
    params: list[Any] = []
    if target_date:
        where.append("trade_date = ?")
        params.append(target_date)
    if news_id:
        where.append("news_id = ?")
        params.append(news_id)
    min_title = int(options.get("min_title_length", 8))
    min_summary = int(options.get("min_summary_length", 20))
    where.append("length(COALESCE(title, '')) >= ?")
    params.append(min_title)
    where.append("length(COALESCE(summary, '')) >= ?")
    params.append(min_summary)
    sql = f"SELECT * FROM v_news_clean WHERE {' AND '.join(where)} ORDER BY trade_date DESC, published_at DESC"
    df = conn.execute(sql, params).df()
    if df.empty:
        return df
    if only_failed and _relation_exists(conn, "v_news_llm_analysis"):
        failed = conn.execute(
            "SELECT DISTINCT news_id FROM v_news_llm_analysis WHERE status='FAILED' AND model_name=?",
            [model_name],
        ).df()
        df = df[df["news_id"].astype(str).isin(set(failed["news_id"].astype(str)))]
    elif not force and _relation_exists(conn, "v_news_llm_analysis"):
        done = conn.execute(
            "SELECT DISTINCT news_id FROM v_news_llm_analysis WHERE status='SUCCESS' AND model_name=? AND prompt_version=? AND analysis_version=?",
            [model_name, PROMPT_VERSION, ANALYSIS_VERSION],
        ).df()
        df = df[~df["news_id"].astype(str).isin(set(done["news_id"].astype(str)))]
    max_articles = int(options.get("daily_max_articles", 150))
    final_limit = min(limit or max_articles, max_articles)
    return df.head(final_limit).reset_index(drop=True)


def _analyze_one(client: LLMClient, profile, item) -> tuple[dict[str, Any], dict[str, Any]]:
    now = pd.Timestamp.now()
    try:
        payload, response = client.chat_json(
            NEWS_EXTRACTION_SYSTEM_PROMPT,
            news_extraction_user_prompt(item.title, item.summary, item.source, str(item.published_at), json.dumps(schema_json(), ensure_ascii=False)),
        )
        model = validate_news_analysis(payload)
        data = model.model_dump() if hasattr(model, "model_dump") else model.dict()
        status = "SUCCESS"
        error_type = ""
        error_message = ""
    except LLMError as exc:
        response = _empty_response()
        data = _failed_schema()
        status = "FAILED"
        error_type = exc.error_type.value
        error_message = exc.message[:500]
    except Exception as exc:
        response = _empty_response()
        data = _failed_schema()
        status = "FAILED"
        error_type = LLMErrorType.SCHEMA_ERROR.value
        error_message = str(exc)[:500]
    row = {
        "analysis_id": uuid.uuid4().hex,
        "news_id": item.news_id,
        "trade_date": item.trade_date,
        "provider": profile.provider,
        "protocol": profile.protocol,
        "model_name": profile.model,
        "prompt_version": PROMPT_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "is_financial_news": bool(data["is_financial_news"]),
        "event_type": data["event_type"],
        "sentiment": data["sentiment"],
        "sentiment_score": float(data["sentiment_score"]),
        "impact_direction": data["impact_direction"],
        "impact_horizon": data["impact_horizon"],
        "importance_score": int(data["importance_score"]),
        "market_relevance": int(data["market_relevance"]),
        "risk_level": data["risk_level"],
        "topics_json": json.dumps(data["topics"], ensure_ascii=False),
        "industries_json": json.dumps(data["industries"], ensure_ascii=False),
        "stocks_json": json.dumps(data["stocks"], ensure_ascii=False),
        "organizations_json": json.dumps(data["organizations"], ensure_ascii=False),
        "countries_regions_json": json.dumps(data["countries_regions"], ensure_ascii=False),
        "summary": str(data["summary"]),
        "key_points_json": json.dumps(data["key_points"], ensure_ascii=False),
        "evidence_json": json.dumps(data["evidence"], ensure_ascii=False),
        "uncertainty": str(data["uncertainty"]),
        "confidence": float(data["confidence"]),
        "rule_tags_json": _rule_tags_json(item.news_id),
        "raw_response": response.raw_response,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "estimated_cost": 0.0,
        "created_at": now,
    }
    usage = {
        "request_id": response.request_id,
        "task_type": "news_extraction",
        "news_id": item.news_id,
        "trade_date": item.trade_date,
        "provider": profile.provider,
        "protocol": profile.protocol,
        "model_name": profile.model,
        "prompt_version": PROMPT_VERSION,
        "status": status,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "retry_count": response.retry_count,
        "estimated_cost": 0.0,
        "error_type": error_type,
        "error_message": error_message,
        "created_at": now,
    }
    return row, usage


def _rule_tags_json(news_id: str) -> str:
    return "[]"


def _failed_schema() -> dict[str, Any]:
    return {
        "is_financial_news": False,
        "event_type": "other",
        "topics": [],
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "impact_direction": "uncertain",
        "impact_horizon": "uncertain",
        "importance_score": 0,
        "market_relevance": 0,
        "risk_level": "UNKNOWN",
        "industries": [],
        "stocks": [],
        "organizations": [],
        "countries_regions": [],
        "summary": "",
        "key_points": [],
        "evidence": [],
        "uncertainty": "failed",
        "confidence": 0.0,
    }


class _empty_response:
    request_id = ""
    raw_response = ""
    input_tokens = None
    output_tokens = None
    latency_ms = 0
    retry_count = 0


def _existing_success(conn: duckdb.DuckDBPyConnection, news_id: str, model_name: str) -> bool:
    if not _relation_exists(conn, "v_news_llm_analysis"):
        return False
    row = conn.execute(
        "SELECT 1 FROM v_news_llm_analysis WHERE news_id=? AND model_name=? AND prompt_version=? AND analysis_version=? AND status='SUCCESS' LIMIT 1",
        [news_id, model_name, PROMPT_VERSION, ANALYSIS_VERSION],
    ).fetchone()
    return row is not None


def _safe_df(conn: duckdb.DuckDBPyConnection, relation: str) -> pd.DataFrame:
    try:
        return conn.execute(f"SELECT * FROM {relation}").df()
    except Exception:
        return pd.DataFrame()


def _relation_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
