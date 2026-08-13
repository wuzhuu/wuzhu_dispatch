from __future__ import annotations

import json
from typing import Any

import pandas as pd


def build_fused_analysis(
    news: pd.DataFrame,
    rule_tags: pd.DataFrame,
    llm_analysis: pd.DataFrame,
    llm_industries: pd.DataFrame,
    llm_stocks: pd.DataFrame,
    fusion_version: str = "fusion_v1",
) -> pd.DataFrame:
    if news.empty:
        return pd.DataFrame()
    rows = []
    now = pd.Timestamp.now()
    for item in news.itertuples(index=False):
        news_id = str(item.news_id)
        rules = _records(rule_tags, news_id)
        llm = llm_analysis[llm_analysis["news_id"].astype(str) == news_id] if not llm_analysis.empty else pd.DataFrame()
        if llm.empty:
            continue
        row = llm.sort_values("created_at", ascending=False).iloc[0]
        topics = _json_list(row.get("topics_json"))
        rule_values = sorted({str(r.get("tag_value")) for r in rules if r.get("tag_value")})
        industries = _records(llm_industries, news_id)
        stocks = _records(llm_stocks, news_id)
        conflicts = []
        if rule_values and topics and not (set(rule_values) & set(topics)):
            conflicts.append("rule_topic_llm_topic_mismatch")
        rows.append(
            {
                "news_id": news_id,
                "trade_date": item.trade_date,
                "rule_tags_json": json.dumps(rules, ensure_ascii=False),
                "llm_topics_json": json.dumps(topics, ensure_ascii=False),
                "final_topics_json": json.dumps(sorted(set(rule_values) | set(topics)), ensure_ascii=False),
                "final_sentiment": row.get("sentiment", "neutral"),
                "final_sentiment_score": row.get("sentiment_score", 0.0),
                "final_risk_level": row.get("risk_level", "UNKNOWN"),
                "final_importance_score": row.get("importance_score", 0),
                "final_market_relevance": row.get("market_relevance", 0),
                "final_industries_json": json.dumps(industries, ensure_ascii=False),
                "final_stocks_json": json.dumps(stocks, ensure_ascii=False),
                "conflict_flags_json": json.dumps(conflicts, ensure_ascii=False),
                "fusion_version": fusion_version,
                "created_at": now,
            }
        )
    return pd.DataFrame(rows)


def _records(df: pd.DataFrame, news_id: str) -> list[dict[str, Any]]:
    if df.empty or "news_id" not in df.columns:
        return []
    out = df[df["news_id"].astype(str) == news_id].copy()
    if out.empty:
        return []
    return out.drop(columns=[c for c in ("created_at", "trade_date", "year", "month") if c in out.columns]).to_dict("records")


def _json_list(value) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except Exception:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
