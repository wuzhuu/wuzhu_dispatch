from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.path_resolver import resolve_data_root
from src.utils.config import add_db_path_arg, resolve_db_path


FORBIDDEN_WORDS = ("买入", "卖出", "推荐买入", "加仓", "减仓", "满仓", "梭哈", "保证收益")
REQUIRED = [
    "news_clean",
    "news_tag_daily",
    "news_industry_link",
    "news_stock_link",
    "daily_news_summary",
    "news_factor_daily",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate RSS news analysis datasets.")
    add_db_path_arg(parser)
    args = parser.parse_args()
    db_path = resolve_db_path(cli_db_path=args.db_path)
    result = validate_news_analysis(db_path)
    for line in result:
        print(line)


def validate_news_analysis(db_path: str | Path) -> list[str]:
    db_path = Path(db_path).expanduser().resolve(strict=False)
    data_root = resolve_data_root(db_path)
    lake_root = data_root / "lake"
    conn = duckdb.connect(str(db_path), read_only=True)
    lines = [f"data_root: {data_root}", f"db_path: {db_path}"]
    try:
        counts = {}
        for dataset in REQUIRED:
            view = f"v_{dataset}"
            count = _count(conn, view)
            counts[dataset] = count
            lines.append(f"{dataset}_rows: {count}")

        raw_news_count = _count(conn, "rss_news")
        duplicate_count = max(raw_news_count - counts.get("news_clean", 0), 0) if raw_news_count else _scalar(conn, "SELECT COUNT(*) FROM v_news_clean WHERE is_duplicate", 0)
        news_count = counts.get("news_clean", 0)
        tag_coverage = _coverage(conn, "v_news_tag_daily", news_count)
        industry_coverage = _coverage(conn, "v_news_industry_link", news_count)
        stock_coverage = _coverage(conn, "v_news_stock_link", news_count)
        forbidden_hits = _forbidden_hits(conn)
        wrong_paths = _project_writes(lake_root)

        lines.extend(
            [
                f"deduplicated_rows: {duplicate_count}",
                f"tag_coverage: {tag_coverage:.2%}",
                f"industry_link_coverage: {industry_coverage:.2%}",
                f"stock_link_coverage: {stock_coverage:.2%}",
                f"daily_news_summary_exists: {counts.get('daily_news_summary', 0) > 0}",
                f"news_factor_daily_exists: {counts.get('news_factor_daily', 0) > 0}",
                f"forbidden_advice_hits: {forbidden_hits}",
                f"project_dir_news_parquet_files: {wrong_paths}",
                f"views_refreshed: {all(_count(conn, f'v_{name}') >= 0 for name in REQUIRED)}",
            ]
        )
        if forbidden_hits:
            lines.append("FAIL: summaries contain advice words.")
        if wrong_paths:
            lines.append("FAIL: news parquet files were found under the project directory.")
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


def _coverage(conn: duckdb.DuckDBPyConnection, relation: str, news_count: int) -> float:
    if news_count <= 0:
        return 0.0
    linked = _scalar(conn, f"SELECT COUNT(DISTINCT news_id) FROM {relation}", 0)
    return float(linked or 0) / news_count


def _forbidden_hits(conn: duckdb.DuckDBPyConnection) -> int:
    hits = 0
    for word in FORBIDDEN_WORDS:
        escaped = word.replace("'", "''")
        hits += int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM v_daily_news_summary "
                f"WHERE market_summary LIKE '%{escaped}%' OR industry_summary LIKE '%{escaped}%'",
                0,
            )
            or 0
        )
    return hits


def _project_writes(lake_root: Path) -> int:
    project_news = list((PROJECT_ROOT / "lake").glob("news_*/**/*.parquet"))
    project_news += list((PROJECT_ROOT / "lake").glob("daily_news_summary/**/*.parquet"))
    if PROJECT_ROOT in lake_root.parents or lake_root == PROJECT_ROOT:
        return 0
    return len(project_news)


if __name__ == "__main__":
    main()
