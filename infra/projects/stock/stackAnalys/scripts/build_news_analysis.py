from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.path_resolver import resolve_data_root
from src.utils.config import add_db_path_arg, resolve_db_path


NEWS_DATASETS = {
    "news_clean": ["news_id"],
    "news_tag_daily": ["news_id", "tag_type", "tag_value"],
    "news_industry_link": ["news_id", "industry_code", "industry_name", "match_keyword"],
    "news_stock_link": ["news_id", "ts_code", "match_type"],
    "daily_news_summary": ["trade_date"],
    "news_factor_daily": ["trade_date", "industry_code", "industry_name"],
}

NEWS_SCHEMAS = {
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
}

FORBIDDEN_ADVICE_WORDS = ("买入", "卖出", "推荐买入", "加仓", "减仓", "满仓", "梭哈", "保证收益")

TAG_RULES = {
    "market_macro": ["宏观", "经济", "央行", "通胀", "利率", "降息", "pmi", "gdp", "inflation", "rate cut", "central bank"],
    "global_market": ["美股", "纳指", "道指", "标普", "海外市场", "全球市场", "fed", "nasdaq", "dow", "s&p", "wall street"],
    "semiconductor": ["半导体", "芯片", "晶圆", "光刻", "先进封装", "存储", "semiconductor", "chip", "wafer", "lithography"],
    "ai_compute": ["ai", "人工智能", "算力", "gpu", "英伟达", "nvidia", "数据中心", "大模型", "accelerator", "compute"],
    "bank_finance": ["银行", "券商", "保险", "金融", "信贷", "bank", "brokerage", "insurance", "finance", "credit"],
    "electric_power": ["电力", "电网", "火电", "水电", "核电", "power grid", "electricity", "utility", "nuclear power"],
    "new_energy": ["新能源", "光伏", "风电", "储能", "锂电", "电池", "ev", "solar", "wind power", "battery", "lithium"],
    "consumer": ["消费", "零售", "白酒", "食品", "旅游", "consumer", "retail", "travel", "liquor", "food"],
    "earnings": ["财报", "业绩", "利润", "营收", "预增", "预减", "earnings", "profit", "revenue", "guidance"],
    "regulation": ["监管", "政策", "规则", "罚款", "调查", "regulation", "policy", "fine", "probe", "investigation"],
    "risk_event": ["风险", "暴跌", "违约", "亏损", "诉讼", "制裁", "召回", "risk", "plunge", "default", "loss", "lawsuit", "sanction", "recall"],
}

FOCUS_INDUSTRY_RULES = {
    "半导体": TAG_RULES["semiconductor"],
    "AI算力": TAG_RULES["ai_compute"],
    "银行": TAG_RULES["bank_finance"],
    "电力": TAG_RULES["electric_power"],
    "新能源": TAG_RULES["new_energy"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build rule-based RSS news analysis datasets.")
    add_db_path_arg(parser)
    parser.add_argument("--date", help="Only build rows whose mapped trade_date equals this date.")
    args = parser.parse_args()

    db_path = resolve_db_path(cli_db_path=args.db_path)
    result = build_news_analysis(db_path, target_date=args.date)
    for line in result["messages"]:
        print(line)


def build_news_analysis(db_path: str | Path, target_date: str | None = None) -> dict[str, object]:
    db_path = Path(db_path).expanduser().resolve(strict=False)
    data_root = resolve_data_root(db_path)
    lake_root = data_root / "lake"
    lake_root.mkdir(parents=True, exist_ok=True)
    messages: list[str] = [f"data_root: {data_root}", f"db_path: {db_path}"]

    conn = duckdb.connect(str(db_path))
    try:
        raw = _read_rss_news(conn)
        if raw.empty:
            _refresh_news_views(conn, lake_root)
            warning = "WARNING: rss_news does not exist or is empty; skipped news analysis."
            messages.append(warning)
            return {"messages": messages, "warning": warning}

        news_clean, duplicate_count = _build_news_clean(raw, target_date)
        tags = _build_tags(news_clean)
        industries = _build_industry_links(conn, news_clean)
        stocks = _build_stock_links(conn, news_clean, industries)
        summaries = _build_daily_summary(news_clean, tags, industries)
        factors = _build_news_factors(industries, tags)

        datasets = {
            "news_clean": news_clean,
            "news_tag_daily": tags,
            "news_industry_link": industries,
            "news_stock_link": stocks,
            "daily_news_summary": summaries,
            "news_factor_daily": factors,
        }
        for dataset, df in datasets.items():
            _append_dataset(lake_root, dataset, df)
        _refresh_news_views(conn, lake_root)

        news_ids = set(news_clean["news_id"].astype(str)) if not news_clean.empty else set()
        tag_coverage = _coverage(tags, news_ids)
        industry_coverage = _coverage(industries, news_ids)
        stock_coverage = _coverage(stocks, news_ids)
        messages.extend(
            [
                f"news_clean_rows: {len(news_clean)}",
                f"deduplicated_rows: {duplicate_count}",
                f"news_tag_daily_rows: {len(tags)}",
                f"news_industry_link_rows: {len(industries)}",
                f"news_stock_link_rows: {len(stocks)}",
                f"daily_news_summary_rows: {len(summaries)}",
                f"news_factor_daily_rows: {len(factors)}",
                f"tag_coverage: {tag_coverage:.2%}",
                f"industry_link_coverage: {industry_coverage:.2%}",
                f"stock_link_coverage: {stock_coverage:.2%}",
                "views_refreshed: v_news_clean, v_news_tag_daily, v_news_industry_link, v_news_stock_link, v_daily_news_summary, v_news_factor_daily",
                "query: SELECT * FROM v_daily_news_summary ORDER BY trade_date DESC LIMIT 5;",
            ]
        )
        return {
            "messages": messages,
            "news_clean_rows": len(news_clean),
            "duplicate_count": duplicate_count,
            "tag_rows": len(tags),
            "industry_rows": len(industries),
            "stock_rows": len(stocks),
            "summary_rows": len(summaries),
            "factor_rows": len(factors),
            "tag_coverage": tag_coverage,
            "industry_coverage": industry_coverage,
            "stock_coverage": stock_coverage,
        }
    finally:
        conn.close()


def _read_rss_news(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    if not _relation_exists(conn, "rss_news"):
        return pd.DataFrame()
    cols = _columns(conn, "rss_news")
    published_col = _first_existing(cols, ["published_at", "publish_time", "collected_time", "fetched_at"])
    id_col = _first_existing(cols, ["news_id", "id", "article_id", "raw_id"])
    select = [
        f"CAST({id_col} AS VARCHAR) AS raw_news_id" if id_col else "NULL AS raw_news_id",
        f"CAST({published_col} AS TIMESTAMP) AS published_at" if published_col else "NULL AS published_at",
        _select_col(cols, "source", ["source", "feed_name"], "source"),
        _select_col(cols, "source_type", ["source_type", "category"], "source_type"),
        _select_col(cols, "title", ["title"], "title"),
        _select_col(cols, "summary", ["summary", "description"], "summary"),
        _select_col(cols, "url", ["url", "link"], "url"),
        _select_col(cols, "language", ["language"], "language"),
    ]
    df = conn.execute(f"SELECT {', '.join(select)} FROM rss_news").df()
    return df


def _build_news_clean(raw: pd.DataFrame, target_date: str | None) -> tuple[pd.DataFrame, int]:
    df = raw.copy()
    now = pd.Timestamp.now()
    for col in ["source", "source_type", "title", "summary", "url", "language"]:
        df[col] = df[col].fillna("").astype(str)
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce").fillna(now)
    df["trade_date"] = df["published_at"].dt.date
    if target_date:
        target = pd.to_datetime(target_date).date()
        df = df[df["trade_date"] == target].copy()
    df["title_norm"] = df["title"].map(_norm_text)
    df["summary_norm"] = df["summary"].map(_norm_text)
    df["duplicate_key"] = df.apply(lambda r: _sha256("|".join([r["title_norm"], r["url"].strip().lower(), r["source"].strip().lower()])), axis=1)
    df = df.sort_values(["duplicate_key", "published_at"], ascending=[True, False])
    df["is_duplicate"] = df.duplicated("duplicate_key", keep="first")
    duplicate_count = int(df["is_duplicate"].sum())
    out = df[~df["is_duplicate"]].copy()
    out["news_id"] = out.apply(lambda r: str(r["raw_news_id"]) if str(r.get("raw_news_id", "")).strip() not in ("", "None", "nan", "<NA>") else _sha256(r["duplicate_key"])[:24], axis=1)
    out["created_at"] = now
    cols = [
        "news_id",
        "published_at",
        "trade_date",
        "source",
        "source_type",
        "title",
        "summary",
        "url",
        "language",
        "title_norm",
        "summary_norm",
        "duplicate_key",
        "is_duplicate",
        "created_at",
    ]
    return out[cols].reset_index(drop=True), duplicate_count


def _build_tags(news: pd.DataFrame) -> pd.DataFrame:
    rows = []
    now = pd.Timestamp.now()
    for row in news.itertuples(index=False):
        text = f"{row.title_norm} {row.summary_norm}"
        for tag, keywords in TAG_RULES.items():
            hits = _keyword_hits(text, keywords)
            if hits:
                confidence = min(0.95, 0.55 + 0.1 * len(hits))
                rows.append(_row(row.news_id, row.trade_date, tag, tag, confidence, "keyword_rule_v1", now))
    return pd.DataFrame(rows, columns=["news_id", "trade_date", "tag_type", "tag_value", "confidence", "method", "created_at"])


def _build_industry_links(conn: duckdb.DuckDBPyConnection, news: pd.DataFrame) -> pd.DataFrame:
    refs = _industry_refs(conn)
    rows = []
    now = pd.Timestamp.now()
    for row in news.itertuples(index=False):
        text = f"{row.title_norm} {row.summary_norm}"
        matched: set[tuple[str, str, str]] = set()
        for focus_name, keywords in FOCUS_INDUSTRY_RULES.items():
            hits = _keyword_hits(text, keywords + [focus_name])
            for ref in _refs_like(refs, focus_name):
                if hits:
                    matched.add((ref["industry_code"], ref["industry_name"], hits[0]))
        for ref in refs:
            name = str(ref["industry_name"])
            if name and _contains(text, name):
                matched.add((ref["industry_code"], name, name))
        for industry_code, industry_name, keyword in sorted(matched):
            rows.append(
                {
                    "news_id": row.news_id,
                    "trade_date": row.trade_date,
                    "industry_code": industry_code,
                    "industry_name": industry_name,
                    "match_keyword": keyword,
                    "confidence": 0.45,
                    "method": "keyword_industry_rule_v1",
                    "created_at": now,
                }
            )
    cols = ["news_id", "trade_date", "industry_code", "industry_name", "match_keyword", "confidence", "method", "created_at"]
    return pd.DataFrame(rows, columns=cols)


def _build_stock_links(conn: duckdb.DuckDBPyConnection, news: pd.DataFrame, industries: pd.DataFrame) -> pd.DataFrame:
    stocks = _stock_refs(conn)
    rows = []
    now = pd.Timestamp.now()
    for item in news.itertuples(index=False):
        title_text = str(item.title_norm)
        summary_text = str(item.summary_norm)
        for stock in stocks:
            name = str(stock.get("name", "")).strip()
            if not name or len(name) < 2:
                continue
            if _contains(title_text, name):
                rows.append(_stock_row(item, stock, "title_name", name, 1.0, now))
            elif _contains(summary_text, name):
                rows.append(_stock_row(item, stock, "summary_name", name, 0.7, now))
    if not industries.empty and stocks:
        industry_pairs = industries[["news_id", "trade_date", "industry_name", "match_keyword"]].drop_duplicates()
        industry_stock = {}
        for stock in stocks:
            industry = str(stock.get("industry_name", "") or stock.get("industry", "")).strip()
            if industry:
                industry_stock.setdefault(industry, []).append(stock)
        for link in industry_pairs.itertuples(index=False):
            candidates = industry_stock.get(str(link.industry_name), [])[:30]
            for stock in candidates:
                rows.append(
                    {
                        "news_id": link.news_id,
                        "trade_date": link.trade_date,
                        "ts_code": stock["ts_code"],
                        "name": stock["name"],
                        "match_type": "industry_weak",
                        "match_keyword": link.match_keyword,
                        "confidence": 0.35,
                        "method": "keyword_stock_rule_v1",
                        "created_at": now,
                    }
                )
    cols = ["news_id", "trade_date", "ts_code", "name", "match_type", "match_keyword", "confidence", "method", "created_at"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols).sort_values("confidence", ascending=False).drop_duplicates(["news_id", "ts_code", "match_type"]).reset_index(drop=True)


def _build_daily_summary(news: pd.DataFrame, tags: pd.DataFrame, industries: pd.DataFrame) -> pd.DataFrame:
    rows = []
    now = pd.Timestamp.now()
    for trade_date, day_news in news.groupby("trade_date"):
        day_tags = tags[tags["trade_date"] == trade_date] if not tags.empty else pd.DataFrame()
        day_industries = industries[industries["trade_date"] == trade_date] if not industries.empty else pd.DataFrame()
        top_tags = _top_counts(day_tags, "tag_value")
        top_industries = _top_counts(day_industries, "industry_name")
        risk_count = int(day_tags[day_tags["tag_value"] == "risk_event"]["news_id"].nunique()) if not day_tags.empty else 0
        source_count = int(day_news["source"].replace("", pd.NA).dropna().nunique())
        rows.append(
            {
                "trade_date": trade_date,
                "source_count": source_count,
                "news_count": int(len(day_news)),
                "top_tags_json": json.dumps(top_tags, ensure_ascii=False),
                "top_industries_json": json.dumps(top_industries, ensure_ascii=False),
                "risk_news_count": risk_count,
                "market_summary": _market_summary(trade_date, len(day_news), top_tags, risk_count),
                "industry_summary": _industry_summary(top_industries),
                "created_at": now,
            }
        )
    return pd.DataFrame(rows)


def _build_news_factors(industries: pd.DataFrame, tags: pd.DataFrame) -> pd.DataFrame:
    cols = ["trade_date", "industry_code", "industry_name", "news_count", "risk_news_count", "news_heat_score", "created_at"]
    if industries.empty:
        return pd.DataFrame(columns=cols)
    risk_news = set(tags[tags["tag_value"] == "risk_event"]["news_id"].astype(str)) if not tags.empty else set()
    rows = []
    now = pd.Timestamp.now()
    grouped = industries.groupby(["trade_date", "industry_code", "industry_name"], dropna=False)
    counts = grouped["news_id"].nunique().reset_index(name="news_count")
    max_by_date = counts.groupby("trade_date")["news_count"].transform("max").replace(0, 1)
    counts["news_heat_score"] = (counts["news_count"] / max_by_date).round(4)
    for row in counts.itertuples(index=False):
        ids = set(industries[(industries["trade_date"] == row.trade_date) & (industries["industry_code"] == row.industry_code)]["news_id"].astype(str))
        rows.append(
            {
                "trade_date": row.trade_date,
                "industry_code": row.industry_code,
                "industry_name": row.industry_name,
                "news_count": int(row.news_count),
                "risk_news_count": len(ids & risk_news),
                "news_heat_score": float(row.news_heat_score),
                "created_at": now,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def _append_dataset(lake_root: Path, dataset: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
    out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
    out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
    for (year, month), part in out.groupby(["year", "month"], dropna=False):
        partition = lake_root / dataset / f"year={year}" / f"month={month}"
        partition.mkdir(parents=True, exist_ok=True)
        path = partition / f"part-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}.parquet"
        part.drop(columns=["year", "month"]).to_parquet(path, index=False)


def _refresh_news_views(conn: duckdb.DuckDBPyConnection, lake_root: Path) -> None:
    for dataset, keys in NEWS_DATASETS.items():
        root = lake_root / dataset
        view = f"v_{dataset}"
        if root.exists() and any(root.rglob("*.parquet")):
            glob = (root / "**" / "*.parquet").as_posix().replace("'", "''")
            key_sql = ", ".join(keys)
            order_sql = "created_at DESC NULLS LAST"
            conn.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true) "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {key_sql} ORDER BY {order_sql}) = 1"
            )
        else:
            conn.execute(f"CREATE OR REPLACE VIEW {view} AS {_empty_select(dataset)}")


def _empty_select(dataset: str) -> str:
    columns = NEWS_SCHEMAS[dataset]
    select_cols = ", ".join(f"CAST(NULL AS {sql_type}) AS {name}" for name, sql_type in columns)
    return f"SELECT {select_cols} WHERE FALSE"


def _industry_refs(conn: duckdb.DuckDBPyConnection) -> list[dict[str, str]]:
    frames = []
    if _relation_exists(conn, "stock_industry_map"):
        frames.append(
            conn.execute(
                "SELECT COALESCE(sw_code_2021, industry_name) AS industry_code, industry_name FROM stock_industry_map WHERE industry_name IS NOT NULL"
            ).df()
        )
    for rel in ["v_industry_board"]:
        if _relation_exists(conn, rel):
            cols = _columns(conn, rel)
            code = _first_existing(cols, ["board_code", "industry_code", "code"])
            name = _first_existing(cols, ["board_name", "industry_name", "name"])
            if name:
                frames.append(conn.execute(f"SELECT COALESCE({code}, {name}) AS industry_code, {name} AS industry_name FROM {rel}").df())
            break
    if not frames:
        return [{"industry_code": name, "industry_name": name} for name in FOCUS_INDUSTRY_RULES]
    refs = pd.concat(frames, ignore_index=True).dropna(subset=["industry_name"]).astype(str)
    refs = refs.drop_duplicates(["industry_code", "industry_name"])
    focus_refs = pd.DataFrame([{"industry_code": name, "industry_name": name} for name in FOCUS_INDUSTRY_RULES])
    refs = pd.concat([refs, focus_refs], ignore_index=True).drop_duplicates(["industry_code", "industry_name"])
    return refs.to_dict("records")


def _stock_refs(conn: duckdb.DuckDBPyConnection) -> list[dict[str, str]]:
    rel = None
    for candidate in ["v_stock_basic_latest", "stock_basic"]:
        if _relation_exists(conn, candidate):
            rel = candidate
            break
    if not rel:
        return []
    cols = _columns(conn, rel)
    ts_col = _first_existing(cols, ["ts_code", "code", "symbol"])
    name_col = _first_existing(cols, ["name", "stock_name"])
    industry_col = _first_existing(cols, ["industry", "industry_name"])
    if not ts_col or not name_col:
        return []
    industry_expr = industry_col if industry_col else "NULL"
    df = conn.execute(f"SELECT {ts_col} AS ts_code, {name_col} AS name, {industry_expr} AS industry_name FROM {rel}").df()
    if _relation_exists(conn, "stock_industry_map"):
        industry = conn.execute("SELECT ts_code, industry_name FROM stock_industry_map WHERE industry_name IS NOT NULL").df()
        df = df.merge(industry, on="ts_code", how="left", suffixes=("", "_map"))
        df["industry_name"] = df["industry_name_map"].fillna(df["industry_name"])
        df = df.drop(columns=[c for c in ["industry_name_map"] if c in df.columns])
    return df.dropna(subset=["ts_code", "name"]).astype(str).drop_duplicates("ts_code").to_dict("records")


def _refs_like(refs: list[dict[str, str]], focus_name: str) -> list[dict[str, str]]:
    related = [ref for ref in refs if focus_name.lower() in str(ref["industry_name"]).lower()]
    if related:
        return related
    return [{"industry_code": focus_name, "industry_name": focus_name}]


def _stock_row(item, stock: dict[str, str], match_type: str, keyword: str, confidence: float, now: pd.Timestamp) -> dict[str, object]:
    return {
        "news_id": item.news_id,
        "trade_date": item.trade_date,
        "ts_code": stock["ts_code"],
        "name": stock["name"],
        "match_type": match_type,
        "match_keyword": keyword,
        "confidence": confidence,
        "method": "keyword_stock_rule_v1",
        "created_at": now,
    }


def _row(news_id: str, trade_date, tag_type: str, tag_value: str, confidence: float, method: str, now: pd.Timestamp) -> dict[str, object]:
    return {
        "news_id": news_id,
        "trade_date": trade_date,
        "tag_type": tag_type,
        "tag_value": tag_value,
        "confidence": confidence,
        "method": method,
        "created_at": now,
    }


def _top_counts(df: pd.DataFrame, col: str) -> list[dict[str, object]]:
    if df.empty or col not in df.columns:
        return []
    counts = df[col].dropna().astype(str).value_counts().head(5)
    return [{"name": idx, "count": int(value)} for idx, value in counts.items()]


def _market_summary(trade_date, news_count: int, top_tags: list[dict[str, object]], risk_count: int) -> str:
    tags = "、".join(f"{item['name']}({item['count']})" for item in top_tags) or "暂无显著主题"
    text = f"{trade_date} 共跟踪到 {news_count} 条新闻，热点主题包括 {tags}。风险事件新闻 {risk_count} 条，建议作为市场解释和风险提示参考。"
    return _strip_forbidden(text)


def _industry_summary(top_industries: list[dict[str, object]]) -> str:
    if not top_industries:
        return "今日暂未形成明确行业新闻热度。"
    industries = "、".join(f"{item['name']}({item['count']})" for item in top_industries)
    return _strip_forbidden(f"行业新闻热度主要集中在 {industries}，均为规则弱关联，不代表个股确定性结论。")


def _strip_forbidden(text: str) -> str:
    for word in FORBIDDEN_ADVICE_WORDS:
        text = text.replace(word, "")
    return text


def _coverage(df: pd.DataFrame, news_ids: set[str]) -> float:
    if not news_ids:
        return 0.0
    if df.empty or "news_id" not in df.columns:
        return 0.0
    return len(set(df["news_id"].astype(str)) & news_ids) / len(news_ids)


def _keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    return [kw for kw in keywords if _contains(text, kw)]


def _contains(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    return str(keyword).lower() in text.lower()


def _norm_text(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    return re.sub(r"\s+", " ", text).strip().lower()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relation_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _columns(conn: duckdb.DuckDBPyConnection, name: str) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute(f"DESCRIBE SELECT * FROM {name} LIMIT 0").fetchall()}
    except Exception:
        return set()


def _first_existing(cols: set[str], candidates: list[str]) -> str | None:
    return next((col for col in candidates if col in cols), None)


def _select_col(cols: set[str], output: str, candidates: list[str], fallback: str) -> str:
    col = _first_existing(cols, candidates)
    return f"CAST({col} AS VARCHAR) AS {output}" if col else f"'' AS {fallback}"


if __name__ == "__main__":
    main()
