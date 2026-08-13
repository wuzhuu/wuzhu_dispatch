from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import duckdb
import pandas as pd


@dataclass
class ResolvedStock:
    ts_code: str
    stock_name: str
    match_confidence: float


@dataclass
class ResolvedIndustry:
    industry_code: str
    industry_name: str
    match_confidence: float


class NewsEntityResolver:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn
        self.stocks = self._load_stocks()
        self.industries = self._load_industries()

    def resolve_stock(self, name: str) -> ResolvedStock | None:
        key = _norm(name)
        if not key:
            return None
        matches = self.stocks[self.stocks["name_norm"] == key]
        if len(matches) == 1:
            row = matches.iloc[0]
            return ResolvedStock(str(row["ts_code"]), str(row["name"]), 1.0)
        return None

    def resolve_industry(self, name: str) -> ResolvedIndustry | None:
        key = _norm(name)
        if not key:
            return None
        exact = self.industries[self.industries["industry_norm"] == key]
        if len(exact) == 1:
            row = exact.iloc[0]
            return ResolvedIndustry(str(row["industry_code"]), str(row["industry_name"]), 1.0)
        contains = self.industries[self.industries["industry_norm"].map(lambda v: key in v or v in key)]
        if len(contains) == 1:
            row = contains.iloc[0]
            return ResolvedIndustry(str(row["industry_code"]), str(row["industry_name"]), 0.75)
        return None

    def stock_links(self, news_id: str, trade_date, stocks: list[dict[str, Any]], model_name: str, analysis_version: str) -> list[dict[str, Any]]:
        rows = []
        now = pd.Timestamp.now()
        for entity in stocks:
            resolved = self.resolve_stock(str(entity.get("name", "")))
            if not resolved:
                continue
            llm_conf = float(entity.get("confidence") or 0.0)
            rows.append(
                {
                    "news_id": news_id,
                    "trade_date": trade_date,
                    "ts_code": resolved.ts_code,
                    "stock_name": resolved.stock_name,
                    "relation": str(entity.get("relation", "")),
                    "llm_confidence": llm_conf,
                    "entity_match_confidence": resolved.match_confidence,
                    "final_confidence": round(min(llm_conf, resolved.match_confidence), 4),
                    "evidence": str(entity.get("evidence", "")),
                    "model_name": model_name,
                    "analysis_version": analysis_version,
                    "created_at": now,
                }
            )
        return rows

    def industry_links(self, news_id: str, trade_date, industries: list[dict[str, Any]], model_name: str, analysis_version: str) -> list[dict[str, Any]]:
        rows = []
        now = pd.Timestamp.now()
        for entity in industries:
            resolved = self.resolve_industry(str(entity.get("name", "")))
            if not resolved:
                continue
            llm_conf = float(entity.get("confidence") or 0.0)
            rows.append(
                {
                    "news_id": news_id,
                    "trade_date": trade_date,
                    "industry_code": resolved.industry_code,
                    "industry_name": resolved.industry_name,
                    "relation": str(entity.get("relation", "")),
                    "llm_confidence": llm_conf,
                    "entity_match_confidence": resolved.match_confidence,
                    "final_confidence": round(min(llm_conf, resolved.match_confidence), 4),
                    "evidence": str(entity.get("evidence", "")),
                    "model_name": model_name,
                    "analysis_version": analysis_version,
                    "created_at": now,
                }
            )
        return rows

    def _load_stocks(self) -> pd.DataFrame:
        relation = _first_relation(self.conn, ["v_stock_basic_latest", "stock_basic"])
        if not relation:
            return pd.DataFrame(columns=["ts_code", "name", "name_norm"])
        cols = _columns(self.conn, relation)
        ts_col = "ts_code" if "ts_code" in cols else "symbol"
        name_col = "name" if "name" in cols else "stock_name"
        df = self.conn.execute(f"SELECT {ts_col} AS ts_code, {name_col} AS name FROM {relation}").df()
        df["name_norm"] = df["name"].map(_norm)
        return df.dropna(subset=["ts_code", "name"]).drop_duplicates("ts_code")

    def _load_industries(self) -> pd.DataFrame:
        frames = []
        if _relation_exists(self.conn, "stock_industry_map"):
            frames.append(
                self.conn.execute(
                    "SELECT COALESCE(sw_code_2021, industry_name) AS industry_code, industry_name FROM stock_industry_map WHERE industry_name IS NOT NULL"
                ).df()
            )
        relation = _first_relation(self.conn, ["v_industry_board"])
        if relation:
            cols = _columns(self.conn, relation)
            code = "board_code" if "board_code" in cols else "industry_code" if "industry_code" in cols else "board_name"
            name = "board_name" if "board_name" in cols else "industry_name" if "industry_name" in cols else "name"
            frames.append(self.conn.execute(f"SELECT COALESCE({code}, {name}) AS industry_code, {name} AS industry_name FROM {relation}").df())
        if not frames:
            return pd.DataFrame(columns=["industry_code", "industry_name", "industry_norm"])
        df = pd.concat(frames, ignore_index=True).dropna(subset=["industry_name"]).astype(str).drop_duplicates()
        df["industry_norm"] = df["industry_name"].map(_norm)
        return df


def _norm(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    return re.sub(r"[\s（）()《》【】\\-_/]+", "", text).lower()


def _relation_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _first_relation(conn: duckdb.DuckDBPyConnection, names: list[str]) -> str | None:
    return next((name for name in names if _relation_exists(conn, name)), None)


def _columns(conn: duckdb.DuckDBPyConnection, name: str) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute(f"DESCRIBE SELECT * FROM {name} LIMIT 0").fetchall()}
    except Exception:
        return set()
