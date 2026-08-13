from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.path_resolver import resolve_data_root
from src.utils.config import add_db_path_arg, resolve_db_path


FORBIDDEN_WORDS = ("买入", "卖出", "目标价", "保证收益", "推荐买入", "满仓", "梭哈")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate LLM news analysis lake outputs.")
    add_db_path_arg(parser)
    args = parser.parse_args()
    db_path = resolve_db_path(cli_db_path=args.db_path)
    for line in validate(db_path):
        print(line)


def validate(db_path: str | Path) -> list[str]:
    db_path = Path(db_path).expanduser().resolve(strict=False)
    data_root = resolve_data_root(db_path)
    conn = duckdb.connect(str(db_path), read_only=True)
    lines = [f"data_root: {data_root}", f"db_path: {db_path}"]
    try:
        analysis_count = _count(conn, "v_news_llm_analysis")
        success_count = _scalar(conn, "SELECT COUNT(*) FROM v_news_llm_analysis WHERE status='SUCCESS'", 0)
        failed_count = _scalar(conn, "SELECT COUNT(*) FROM v_news_llm_analysis WHERE status='FAILED'", 0)
        duplicate_keys = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT news_id, model_name, prompt_version, analysis_version, COUNT(*) AS c
                FROM v_news_llm_analysis
                GROUP BY news_id, model_name, prompt_version, analysis_version
                HAVING c > 1
            )
            """,
            0,
        )
        stock_links = _count(conn, "v_news_llm_stock_link")
        industry_links = _count(conn, "v_news_llm_industry_link")
        invalid_codes = _scalar(conn, "SELECT COUNT(*) FROM v_news_llm_stock_link WHERE ts_code IS NULL OR ts_code=''", 0)
        empty_evidence = _ratio(conn, "v_news_llm_analysis", "evidence_json IS NULL OR evidence_json IN ('[]', '')")
        forbidden_hits = _forbidden_hits(conn)
        key_leaks = _key_leaks(conn)
        wrong_paths = _project_artifacts(data_root)
        schema_ok = success_count
        schema_rate = schema_ok / analysis_count if analysis_count else 0.0
        success_rate = success_count / analysis_count if analysis_count else 0.0
        stock_resolve_rate = stock_links / success_count if success_count else 0.0
        industry_resolve_rate = industry_links / success_count if success_count else 0.0
        lines.extend(
            [
                f"news_llm_analysis_rows: {analysis_count}",
                f"success_count: {success_count}",
                f"failed_count: {failed_count}",
                f"success_rate: {success_rate:.2%}",
                f"json_schema_valid_rate: {schema_rate:.2%}",
                f"duplicate_unique_keys: {duplicate_keys}",
                f"stock_entity_resolve_rate: {stock_resolve_rate:.2%}",
                f"industry_entity_resolve_rate: {industry_resolve_rate:.2%}",
                f"invalid_stock_code_count: {invalid_codes}",
                f"empty_evidence_ratio: {empty_evidence:.2%}",
                f"raw_response_rows: {_scalar(conn, 'SELECT COUNT(*) FROM v_news_llm_analysis WHERE raw_response IS NOT NULL', 0)}",
                f"api_key_leak_hits: {key_leaks}",
                f"daily_news_llm_digest_rows: {_count(conn, 'v_daily_news_llm_digest')}",
                f"news_fused_analysis_rows: {_count(conn, 'v_news_fused_analysis')}",
                f"llm_usage_log_rows: {_count(conn, 'v_llm_usage_log')}",
                f"forbidden_advice_hits: {forbidden_hits}",
                f"project_dir_analysis_artifacts: {wrong_paths}",
                f"results_in_data_root: {wrong_paths == 0}",
            ]
        )
        return lines
    finally:
        conn.close()


def _count(conn: duckdb.DuckDBPyConnection, relation: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0])
    except Exception:
        return 0


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str, default):
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return default


def _ratio(conn: duckdb.DuckDBPyConnection, relation: str, predicate: str) -> float:
    total = _count(conn, relation)
    if total <= 0:
        return 0.0
    count = _scalar(conn, f"SELECT COUNT(*) FROM {relation} WHERE {predicate}", 0)
    return float(count or 0) / total


def _forbidden_hits(conn: duckdb.DuckDBPyConnection) -> int:
    hits = 0
    for relation, columns in {
        "v_daily_news_llm_digest": ["market_summary", "report_markdown", "risk_summary", "stock_summary"],
        "v_news_llm_analysis": ["summary", "raw_response"],
    }.items():
        for word in FORBIDDEN_WORDS:
            escaped = word.replace("'", "''")
            cond = " OR ".join(f"{col} LIKE '%{escaped}%'" for col in columns)
            hits += int(_scalar(conn, f"SELECT COUNT(*) FROM {relation} WHERE {cond}", 0) or 0)
    return hits


def _key_leaks(conn: duckdb.DuckDBPyConnection) -> int:
    return int(
        _scalar(
            conn,
            "SELECT COUNT(*) FROM v_news_llm_analysis WHERE raw_response LIKE '%Bearer %' OR raw_response LIKE '%sk-%' OR raw_response LIKE '%OPENCODE_GO_API_KEY%'",
            0,
        )
        or 0
    )


def _project_artifacts(data_root: Path) -> int:
    patterns = ["news_llm_analysis", "llm_usage_log", "news_llm_stock_link", "news_llm_industry_link", "news_fused_analysis", "daily_news_llm_digest"]
    count = 0
    for name in patterns:
        count += len(list((PROJECT_ROOT / "lake" / name).glob("**/*.parquet")))
        count += len(list((PROJECT_ROOT / name).glob("**/*.parquet")))
    return count


if __name__ == "__main__":
    main()
