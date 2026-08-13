from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.news_lake_writer import append_dataset, refresh_news_llm_views
from src.llm.client import LLMClient
from src.llm.config import load_llm_config
from src.llm.exceptions import LLMError
from src.llm.prompts import DAILY_DIGEST_SYSTEM_PROMPT, daily_digest_user_prompt
from src.utils.config import add_db_path_arg, resolve_db_path


PROMPT_VERSION = "daily_news_digest_v2"
DIGEST_VERSION = "daily_news_llm_digest_v1"
FORBIDDEN_WORDS = ("买入", "卖出", "目标价", "保证收益", "推荐买入", "满仓", "梭哈")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily LLM news digest.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="../key/stackAnalys_llm.yaml")
    parser.add_argument("--profile", default="digest")
    parser.add_argument("--date")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    db_path = resolve_db_path(cli_db_path=args.db_path)
    result = generate_digest(db_path, args.config, args.profile, args.date, args.dry_run, args.force)
    for line in result:
        print(line)


def generate_digest(
    db_path: str | Path,
    config_path: str | Path,
    profile_name: str = "digest",
    target_date: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    db_path = Path(db_path).expanduser().resolve(strict=False)
    conn = duckdb.connect(str(db_path))
    try:
        trade_date = target_date or _latest_date(conn)
        payload = _aggregate_payload(conn, trade_date)
        lines = [f"trade_date: {trade_date}", f"source_news_count: {payload['source_news_count']}", f"analyzed_news_count: {payload['analyzed_news_count']}"]
        if dry_run:
            return lines + ["dry_run: true", "called_provider: false"]
        if not force and _existing_digest(conn, trade_date):
            refresh_news_llm_views(db_path)
            return lines + ["skipped_existing_digest: true"]
        try:
            cfg = load_llm_config(config_path)
            profile = getattr(cfg, profile_name)
        except FileNotFoundError as exc:
            profile = None
            llm_payload = None
            raw_response = ""
            status = "FALLBACK"
            input_tokens = output_tokens = None
            latency_ms = 0
            retry_count = 0
            error_type = "CONFIG_MISSING"
            error_message = str(exc)
            lines.append(f"WARNING: {exc}; using rule daily_news_summary fallback.")
        else:
            if not cfg.enabled or (not profile.api_key and not profile.mock):
                llm_payload = None
                raw_response = ""
                status = "FALLBACK"
                input_tokens = output_tokens = None
                latency_ms = 0
                retry_count = 0
                error_type = "DISABLED" if not cfg.enabled else "AUTH_ERROR"
                error_message = "llm.enabled=false" if not cfg.enabled else f"missing API key from {profile.api_key_source_label}"
                if not cfg.enabled:
                    lines.append("WARNING: llm.enabled=false; using rule daily_news_summary fallback.")
                else:
                    lines.append(f"WARNING: missing API key from {profile.api_key_source_label}; using rule daily_news_summary fallback.")
            else:
                client = LLMClient(profile)
                try:
                    llm_payload, response = client.chat_json(DAILY_DIGEST_SYSTEM_PROMPT, daily_digest_user_prompt(json.dumps(payload, ensure_ascii=False, default=str)))
                    raw_response = response.raw_response
                    status = "SUCCESS"
                    input_tokens = response.input_tokens
                    output_tokens = response.output_tokens
                    latency_ms = response.latency_ms
                    retry_count = response.retry_count
                    error_type = ""
                    error_message = ""
                except LLMError as exc:
                    llm_payload = None
                    raw_response = ""
                    status = "FALLBACK"
                    input_tokens = output_tokens = None
                    latency_ms = 0
                    retry_count = 0
                    error_type = exc.error_type.value
                    error_message = exc.message
                    lines.append(f"WARNING: digest LLM failed {exc.error_type.value}; using fallback.")
        row = _digest_row(trade_date, payload, profile, llm_payload, raw_response, status, input_tokens, output_tokens, latency_ms)
        append_dataset(db_path, "daily_news_llm_digest", pd.DataFrame([row]))
        append_dataset(
            db_path,
            "llm_usage_log",
            pd.DataFrame(
                [
                    {
                        "request_id": row["digest_id"],
                        "task_type": "daily_news_digest",
                        "news_id": "",
                        "trade_date": row["trade_date"],
                        "provider": row["provider"],
                        "protocol": getattr(profile, "protocol", "fallback") if profile else "fallback",
                        "model_name": row["model_name"],
                        "prompt_version": PROMPT_VERSION,
                        "status": status,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "latency_ms": latency_ms,
                        "retry_count": retry_count,
                        "estimated_cost": 0.0,
                        "error_type": error_type,
                        "error_message": error_message[:500],
                        "created_at": pd.Timestamp.now(),
                    }
                ]
            ),
        )
        refresh_news_llm_views(db_path)
        lines.extend([f"status: {status}", "daily_news_llm_digest_rows_written: 1"])
        return lines
    finally:
        conn.close()


def _aggregate_payload(conn: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    summary = _safe_df(conn, f"SELECT * FROM v_daily_news_summary WHERE trade_date = DATE '{trade_date}'")
    analyses = _safe_df(conn, f"SELECT * FROM v_news_llm_analysis WHERE trade_date = DATE '{trade_date}' AND status='SUCCESS' ORDER BY importance_score DESC, market_relevance DESC LIMIT 80")
    fused = _safe_df(conn, f"SELECT * FROM v_news_fused_analysis WHERE trade_date = DATE '{trade_date}'")
    market = _safe_df(conn, f"SELECT * FROM v_market_state_daily WHERE trade_date <= DATE '{trade_date}' ORDER BY trade_date DESC LIMIT 1")
    scores = _safe_df(conn, f"SELECT ts_code, name, industry, rank, total_score, risk_level FROM v_score_daily WHERE trade_date <= DATE '{trade_date}' ORDER BY trade_date DESC, rank LIMIT 20")
    return {
        "trade_date": trade_date,
        "source_news_count": int(summary["news_count"].iloc[0]) if not summary.empty and "news_count" in summary else 0,
        "analyzed_news_count": len(analyses),
        "failed_news_count": int(_scalar(conn, f"SELECT COUNT(*) FROM v_news_llm_analysis WHERE trade_date = DATE '{trade_date}' AND status='FAILED'", 0)),
        "rule_summary": summary.to_dict("records"),
        "llm_news": analyses.head(30).to_dict("records"),
        "fused": fused.head(60).to_dict("records"),
        "market_state": market.to_dict("records"),
        "score_top20": scores.to_dict("records"),
    }


def _digest_row(trade_date: str, payload: dict[str, Any], profile, llm_payload: dict[str, Any] | None, raw_response: str, status: str, input_tokens, output_tokens, latency_ms: int) -> dict[str, Any]:
    llm_payload = llm_payload or {}
    fallback = _fallback_text(payload)
    report = _strip_forbidden(str(llm_payload.get("report_markdown") or fallback))
    return {
        "digest_id": uuid.uuid4().hex,
        "trade_date": pd.to_datetime(trade_date).date(),
        "provider": getattr(profile, "provider", "fallback") if profile else "fallback",
        "model_name": getattr(profile, "model", "keyword_rule_fallback") if profile else "keyword_rule_fallback",
        "prompt_version": PROMPT_VERSION,
        "digest_version": DIGEST_VERSION,
        "source_news_count": payload["source_news_count"],
        "analyzed_news_count": payload["analyzed_news_count"],
        "failed_news_count": payload["failed_news_count"],
        "market_regime": _market_regime(payload),
        "top_topics_json": _json_dumps(_top_from_analysis(payload, "topics_json")),
        "top_industries_json": _json_dumps(_top_from_fused(payload, "final_industries_json")),
        "top_stocks_json": _json_dumps(_top_from_fused(payload, "final_stocks_json")),
        "top_events_json": _json_dumps(_top_events(payload)),
        "risk_events_json": _json_dumps(_risk_events(payload)),
        "market_summary": _strip_forbidden(str(llm_payload.get("market_summary") or fallback)),
        "macro_summary": _strip_forbidden(str(llm_payload.get("macro_summary") or "")),
        "industry_summary": _strip_forbidden(str(llm_payload.get("industry_summary") or "")),
        "stock_summary": _strip_forbidden(str(llm_payload.get("stock_summary") or "")),
        "risk_summary": _strip_forbidden(str(llm_payload.get("risk_summary") or "")),
        "uncertainty_summary": _strip_forbidden(str(llm_payload.get("uncertainty_summary") or "信息来自规则和结构化新闻层，存在覆盖不足和来源延迟。")),
        "report_markdown": report,
        "raw_response": raw_response,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "created_at": pd.Timestamp.now(),
    }


def _latest_date(conn: duckdb.DuckDBPyConnection) -> str:
    for rel in ("v_news_llm_analysis", "v_daily_news_summary", "v_news_clean"):
        try:
            row = conn.execute(f"SELECT MAX(trade_date) FROM {rel}").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            continue
    return str(pd.Timestamp.now().date())


def _existing_digest(conn: duckdb.DuckDBPyConnection, trade_date: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM v_daily_news_llm_digest WHERE trade_date=? LIMIT 1", [trade_date]).fetchone() is not None
    except Exception:
        return False


def _fallback_text(payload: dict[str, Any]) -> str:
    summary = payload.get("rule_summary") or []
    if summary:
        row = summary[0]
        return _strip_forbidden(f"# 每日新闻摘要\n\n{row.get('market_summary', '')}\n\n{row.get('industry_summary', '')}\n")
    return "# 每日新闻摘要\n\n今日暂无可用新闻摘要。\n"


def _market_regime(payload: dict[str, Any]) -> str:
    rows = payload.get("market_state") or []
    return str(rows[0].get("market_regime", "")) if rows else ""


def _top_from_analysis(payload: dict[str, Any], column: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in payload.get("llm_news", []):
        for item in _loads(row.get(column)):
            counts[str(item)] = counts.get(str(item), 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]]


def _top_from_fused(payload: dict[str, Any], column: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in payload.get("fused", []):
        for item in _loads(row.get(column)):
            name = item.get("industry_name") or item.get("stock_name") or item.get("name") if isinstance(item, dict) else str(item)
            counts[str(name)] = counts.get(str(name), 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10] if k]


def _top_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    counts = {}
    for row in payload.get("llm_news", []):
        event = str(row.get("event_type", "other"))
        counts[event] = counts.get(event, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]]


def _risk_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload.get("llm_news", []) if row.get("risk_level") in {"HIGH", "MEDIUM"}][:20]


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False)


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _loads(value) -> list[Any]:
    try:
        data = json.loads(value or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _strip_forbidden(text: str) -> str:
    for word in FORBIDDEN_WORDS:
        text = text.replace(word, "")
    return text


def _safe_df(conn: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    try:
        return conn.execute(sql).df()
    except Exception:
        return pd.DataFrame()


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str, default):
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return default


if __name__ == "__main__":
    main()
