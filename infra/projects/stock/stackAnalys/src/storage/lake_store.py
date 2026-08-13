from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from src.utils.config import load_settings, resolve_db_path, resolve_lake_root, resolve_path

logger = logging.getLogger(__name__)


SOURCE_PRIORITY = {
    "sina_spot_daily": 120,
    "sina_history_direct": 115,
    "baostock": 100,
    "akshare": 80,
    "tencent": 75,
    "westock": 70,
    "equal_data": 65,
    "yahoo": 60,
    "stooq": 50,
}


DAILY_PRICE_COLUMNS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "pct_chg",
    "turn",
    "tradestatus",
    "is_st",
    "adjust_type",
    "source",
    "source_priority",
    "updated_at",
]

INDEX_DAILY_COLUMNS = [
    "index_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "pct_chg",
    "source",
    "source_priority",
    "updated_at",
]

STOCK_BASIC_COLUMNS = [
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "market",
    "list_date",
    "exchange",
    "is_hs",
    "updated_at",
    "snapshot_date",
    "source",
    "source_priority",
]

INDUSTRY_BOARD_COLUMNS = [
    "board_code",
    "board_name",
    "board_type",
    "latest_price",
    "change_amount",
    "pct_chg",
    "total_market_value",
    "turnover_rate",
    "rising_count",
    "falling_count",
    "leading_stock",
    "leading_stock_pct_chg",
    "updated_at",
    "snapshot_date",
    "source",
    "source_priority",
]

STOCK_INDUSTRY_MAP_COLUMNS = [
    "ts_code",
    "name",
    "industry_source",
    "industry_code_l1",
    "industry_name_l1",
    "industry_code_l2",
    "industry_name_l2",
    "industry_code_l3",
    "industry_name_l3",
    "industry_name",
    "sw_code_2021",
    "source",
    "effective_date",
    "is_current",
    "created_at",
    "updated_at",
    "fetched_at",
]

INDUSTRY_BOARD_LOCAL_COLUMNS = [
    "trade_date",
    "industry_source",
    "industry_level",
    "industry_code",
    "industry_name",
    "member_count",
    "valid_member_count",
    "unmapped_count",
    "up_count",
    "down_count",
    "flat_count",
    "avg_ret_1d",
    "median_ret_1d",
    "avg_ret_5d",
    "median_ret_5d",
    "avg_ret_20d",
    "median_ret_20d",
    "amount_sum",
    "volume_sum",
    "amount_ma20",
    "breadth_up_ratio",
    "industry_momentum_score",
    "industry_breadth_score",
    "industry_liquidity_score",
    "industry_strength_score",
    "data_quality_flags",
    "source",
    "created_at",
]

INDUSTRY_BOARD_VIEW_COLUMNS = INDUSTRY_BOARD_LOCAL_COLUMNS

INDUSTRY_VALIDATION_COLUMNS = [
    "validation_id",
    "validated_at",
    "db_path",
    "data_root",
    "check_name",
    "status",
    "issue_level",
    "message",
    "value",
    "threshold",
    "created_at",
]

SW_INDUSTRY_INDEX_COLUMNS = [
    "trade_date",
    "industry_code",
    "industry_name",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "pct_chg",
    "volume",
    "amount",
    "source",
    "created_at",
]


class LakeStore:
    """Parquet-first storage with DuckDB metadata and query views."""

    def __init__(self, db_path: str | Path | None = None, lake_root: str | Path | None = None):
        settings = load_settings()
        self.db_path = resolve_path(db_path) if db_path else resolve_db_path(settings)
        if lake_root:
            self.lake_root = resolve_path(lake_root)
        else:
            data_root = self.db_path.parent.parent if self.db_path.parent.name == "db" else self.db_path.parent
            self.lake_root = resolve_lake_root(settings, str(data_root) if db_path else None)
        self.backup_root = self.lake_root / "backups"
        self.conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lake_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        if self.conn is None:
            self.conn = duckdb.connect(str(self.db_path))
            self.init_metadata()
        return self.conn

    def execute(self, sql: str, parameters: Iterable[object] | None = None):
        conn = self.connect()
        return conn.execute(sql) if parameters is None else conn.execute(sql, parameters)

    def _ensure_columns(self, table_name: str, columns: dict[str, str]) -> None:
        conn = self.conn or self.connect()
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()}
        for column, sql_type in columns.items():
            if column not in existing:
                conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column}" {sql_type}')

    def init_metadata(self) -> None:
        conn = self.conn or duckdb.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_manifest (
                dataset_name VARCHAR,
                partition_path VARCHAR,
                partition_spec VARCHAR,
                row_count BIGINT,
                file_count BIGINT,
                min_trade_date DATE,
                max_trade_date DATE,
                snapshot_date DATE,
                content_hash VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_job (
                job_id VARCHAR PRIMARY KEY,
                dataset_name VARCHAR,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                status VARCHAR,
                parameters VARCHAR,
                message VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_record (
                job_id VARCHAR,
                dataset_name VARCHAR,
                partition_path VARCHAR,
                records_in BIGINT,
                records_written BIGINT,
                status VARCHAR,
                message VARCHAR,
                created_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_coverage (
                dataset_name VARCHAR,
                symbol VARCHAR,
                adjust_type VARCHAR,
                min_trade_date DATE,
                max_trade_date DATE,
                row_count BIGINT,
                updated_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unique_key_index (
                dataset_name VARCHAR,
                unique_key VARCHAR,
                partition_path VARCHAR,
                source VARCHAR,
                source_priority INTEGER,
                updated_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_update_log (
                log_time TIMESTAMP,
                task_name VARCHAR,
                target VARCHAR,
                status VARCHAR,
                message VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS data_quality_log (
                check_time VARCHAR,
                table_name VARCHAR,
                trade_date VARCHAR,
                check_name VARCHAR,
                status VARCHAR,
                message VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rss_news_articles (
                article_id VARCHAR PRIMARY KEY,
                feed_name VARCHAR,
                title VARCHAR,
                summary VARCHAR,
                url VARCHAR,
                published_at TIMESTAMP,
                fetched_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rss_news (
                id              VARCHAR PRIMARY KEY,
                title           VARCHAR,
                summary         VARCHAR,
                source          VARCHAR,
                category        VARCHAR,
                url             VARCHAR,
                publish_time    TIMESTAMP WITH TIME ZONE,
                collected_time  TIMESTAMP WITH TIME ZONE,
                source_type     VARCHAR,
                language        VARCHAR,
                raw_id          VARCHAR,
                content_hash    VARCHAR
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rss_news_url ON rss_news(url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rss_news_hash ON rss_news(content_hash)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rss_news_source_health (
                source_id        VARCHAR PRIMARY KEY,
                status           VARCHAR,
                consecutive_fail INT DEFAULT 0,
                last_error       VARCHAR,
                last_http_code   INT,
                last_ok_at       TIMESTAMP,
                last_fail_at     TIMESTAMP,
                disabled_until   TIMESTAMP,
                schema_drift_count INT DEFAULT 0,
                field_completeness JSON,
                response_snippet VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_industry_map (
                ts_code TEXT,
                name TEXT,
                industry_source TEXT,
                industry_code_l1 TEXT,
                industry_name_l1 TEXT,
                industry_code_l2 TEXT,
                industry_name_l2 TEXT,
                industry_code_l3 TEXT,
                industry_name_l3 TEXT,
                industry_name TEXT,
                sw_code_2021 TEXT,
                source TEXT,
                effective_date DATE,
                is_current BOOLEAN,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                fetched_at TIMESTAMP
            )
            """
        )
        self._ensure_columns(
            "stock_industry_map",
            {
                "name": "TEXT",
                "industry_source": "TEXT",
                "industry_code_l1": "TEXT",
                "industry_name_l1": "TEXT",
                "industry_code_l2": "TEXT",
                "industry_name_l2": "TEXT",
                "industry_code_l3": "TEXT",
                "industry_name_l3": "TEXT",
                "effective_date": "DATE",
                "is_current": "BOOLEAN",
                "created_at": "TIMESTAMP",
                "updated_at": "TIMESTAMP",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS data_source_status (
                source VARCHAR,
                dataset_name VARCHAR,
                latest_remote_date DATE,
                latest_local_date DATE,
                status VARCHAR,
                message VARCHAR,
                checked_at TIMESTAMP,
                PRIMARY KEY (source, dataset_name)
            )
            """
        )
        self._ensure_columns(
            "data_source_status",
            {
                "trade_date": "DATE",
                "source_name": "VARCHAR",
                "total_requests": "BIGINT",
                "success_count": "BIGINT",
                "fail_count": "BIGINT",
                "success_rate": "DOUBLE",
                "avg_latency_ms": "DOUBLE",
                "p95_latency_ms": "DOUBLE",
                "actual_qps": "DOUBLE",
                "peak_qps": "DOUBLE",
                "field_missing_rate": "DOUBLE",
                "date_mismatch_count": "BIGINT",
                "suspended_count": "BIGINT",
                "unsupported_market_count": "BIGINT",
                "error_summary": "VARCHAR",
                "started_at": "TIMESTAMP",
                "finished_at": "TIMESTAMP",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS missing_daily_price (
                trade_date DATE,
                code VARCHAR,
                source_name VARCHAR,
                fail_reason VARCHAR,
                retry_count INTEGER,
                status VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (trade_date, code, source_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_price_temp (
                ts_code VARCHAR,
                trade_date DATE,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                preclose DOUBLE,
                volume DOUBLE,
                amount DOUBLE,
                pct_chg DOUBLE,
                turn DOUBLE,
                tradestatus VARCHAR,
                is_st VARCHAR,
                adjust_type VARCHAR,
                source VARCHAR,
                source_priority INTEGER,
                status VARCHAR,
                updated_at TIMESTAMP,
                PRIMARY KEY (ts_code, trade_date, adjust_type, source)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_collection_checkpoint (
                trade_date DATE,
                source_name VARCHAR,
                batch_key VARCHAR,
                status VARCHAR,
                symbol_count INTEGER,
                price_rows BIGINT,
                missing_rows BIGINT,
                message VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (trade_date, source_name, batch_key)
            )
            """
        )

    def upsert_dataframe(self, table_name: str, df: pd.DataFrame, primary_keys: list[str]) -> int:
        """Compatibility helper for metadata/log tables only."""
        if df is None or df.empty:
            return 0
        if table_name == "stock_basic":
            return self.write_stock_basic_snapshot(df)
        if table_name == "industry_board":
            return self.write_industry_board_snapshot(df)
        if table_name == "stock_industry_map":
            return self.upsert_stock_industry_map(df)
        if table_name == "daily_price":
            return self.upsert_daily_price(df)
        if table_name == "index_daily":
            return self.upsert_index_daily(df)
        conn = self.connect()
        temp_name = f"tmp_{table_name}_{uuid.uuid4().hex[:8]}"
        columns = list(df.columns)
        quoted_columns = ", ".join(f'"{col}"' for col in columns)
        join_clause = " AND ".join(f't."{key}" = s."{key}"' for key in primary_keys) if primary_keys else ""
        conn.register(temp_name, df)
        try:
            # 先删后插实现 upsert：DELETE 匹配的行，再 INSERT
            # 对无主键的表（如 data_quality_log）跳过 DELETE，直接 INSERT
            if primary_keys:
                conn.execute(f'DELETE FROM "{table_name}" AS t USING "{temp_name}" AS s WHERE {join_clause}')
            conn.execute(f'INSERT INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{temp_name}"')
        except Exception:
            # 个别表有约束冲突（如 missing_daily_price 的 sys_auth 记录），
            # 不影响主流程，记录警告后继续
            logger.warning("upsert_dataframe failed for %s (pk=%s): will retry with INSERT OR IGNORE", table_name, primary_keys)
            conn.execute(f'INSERT OR IGNORE INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{temp_name}"')
        finally:
            conn.unregister(temp_name)
        return len(df)

    def upsert_daily_price_temp(self, df: pd.DataFrame, status: str = "intraday") -> int:
        if df is None or df.empty:
            return 0
        out = self._prepare_daily_price(df, "qfq")
        out["status"] = status
        cols = DAILY_PRICE_COLUMNS + ["status"]
        return self.upsert_dataframe("daily_price_temp", out[cols], ["ts_code", "trade_date", "adjust_type", "source"])

    def upsert_missing_daily_price(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        out = df.copy()
        now = pd.Timestamp.now()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["code"] = out["code"].astype(str)
        out["source_name"] = out["source_name"].astype(str)
        if "retry_count" in out.columns:
            out["retry_count"] = pd.to_numeric(out["retry_count"], errors="coerce").fillna(0).astype("int64")
        else:
            out["retry_count"] = 0
        out["status"] = out["status"] if "status" in out.columns else "pending"
        out["created_at"] = pd.to_datetime(out["created_at"] if "created_at" in out.columns else now, errors="coerce")
        out["updated_at"] = pd.to_datetime(out["updated_at"] if "updated_at" in out.columns else now, errors="coerce")
        out["created_at"] = out["created_at"].fillna(now) if hasattr(out["created_at"], "fillna") else now
        out["updated_at"] = out["updated_at"].fillna(now) if hasattr(out["updated_at"], "fillna") else now
        cols = ["trade_date", "code", "source_name", "fail_reason", "retry_count", "status", "created_at", "updated_at"]
        for col in cols:
            if col not in out.columns:
                out[col] = pd.NA
        return self.upsert_dataframe("missing_daily_price", out[cols], ["trade_date", "code", "source_name"])

    def record_data_source_metrics(self, stats: dict[str, object]) -> None:
        source_name = str(stats.get("source_name", ""))
        trade_date = pd.to_datetime(stats.get("trade_date"), errors="coerce").date()
        checked_at = pd.Timestamp.now()
        self.execute(
            "DELETE FROM data_source_status WHERE source = ? AND dataset_name = ?",
            [source_name, "daily_price"],
        )
        self.execute(
            """
            INSERT INTO data_source_status (
                source, dataset_name, latest_remote_date, latest_local_date, status, message, checked_at,
                trade_date, source_name, total_requests, success_count, fail_count, success_rate,
                avg_latency_ms, p95_latency_ms, actual_qps, peak_qps, field_missing_rate,
                date_mismatch_count, suspended_count, unsupported_market_count, error_summary,
                started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source_name,
                "daily_price",
                trade_date,
                trade_date,
                "SUCCESS" if float(stats.get("success_rate", 0.0)) > 0 else "FAIL",
                str(stats.get("error_summary", "")),
                checked_at,
                trade_date,
                source_name,
                int(stats.get("total_requests", 0)),
                int(stats.get("success_count", 0)),
                int(stats.get("fail_count", 0)),
                float(stats.get("success_rate", 0.0)),
                float(stats.get("avg_latency_ms", 0.0)),
                float(stats.get("p95_latency_ms", 0.0)),
                float(stats.get("actual_qps", 0.0)),
                float(stats.get("peak_qps", 0.0)),
                float(stats.get("field_missing_rate", 0.0)),
                int(stats.get("date_mismatch_count", 0)),
                int(stats.get("suspended_count", 0)),
                int(stats.get("unsupported_market_count", 0)),
                str(stats.get("error_summary", "")),
                pd.to_datetime(stats.get("started_at"), errors="coerce"),
                pd.to_datetime(stats.get("finished_at"), errors="coerce"),
            ],
        )

    def mark_missing_daily_price_repaired(self, codes: list[str], trade_date: str, source_name: str = "sina_spot_daily") -> None:
        if not codes:
            return
        conn = self.connect()
        rows = [(pd.to_datetime(trade_date).date(), str(code), source_name, pd.Timestamp.now()) for code in codes]
        conn.executemany(
            """
            UPDATE missing_daily_price
            SET status = 'repaired', updated_at = ?
            WHERE trade_date = ? AND code = ? AND source_name = ?
            """,
            [(updated_at, date_value, code, source) for date_value, code, source, updated_at in rows],
        )

    def completed_collection_batches(self, trade_date: str, source_name: str) -> set[str]:
        rows = self.execute(
            """
            SELECT batch_key
            FROM daily_collection_checkpoint
            WHERE trade_date = ? AND source_name = ? AND status = 'completed'
            """,
            [pd.to_datetime(trade_date).date(), source_name],
        ).fetchall()
        return {str(row[0]) for row in rows}

    def upsert_collection_checkpoint(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        out = df.copy()
        now = pd.Timestamp.now()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["source_name"] = out["source_name"].astype(str)
        out["batch_key"] = out["batch_key"].astype(str)
        out["status"] = out["status"].astype(str)
        out["symbol_count"] = pd.to_numeric(out["symbol_count"], errors="coerce").fillna(0).astype("int64")
        out["price_rows"] = pd.to_numeric(out["price_rows"], errors="coerce").fillna(0).astype("int64")
        out["missing_rows"] = pd.to_numeric(out["missing_rows"], errors="coerce").fillna(0).astype("int64")
        out["created_at"] = pd.to_datetime(out["created_at"] if "created_at" in out.columns else now, errors="coerce")
        out["updated_at"] = pd.to_datetime(out["updated_at"] if "updated_at" in out.columns else now, errors="coerce")
        out["created_at"] = out["created_at"].fillna(now) if hasattr(out["created_at"], "fillna") else now
        out["updated_at"] = out["updated_at"].fillna(now) if hasattr(out["updated_at"], "fillna") else now
        cols = ["trade_date", "source_name", "batch_key", "status", "symbol_count", "price_rows", "missing_rows", "message", "created_at", "updated_at"]
        for col in cols:
            if col not in out.columns:
                out[col] = pd.NA
        return self.upsert_dataframe("daily_collection_checkpoint", out[cols], ["trade_date", "source_name", "batch_key"])

    def upsert_stock_industry_map(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        out = df.copy()
        cols = STOCK_INDUSTRY_MAP_COLUMNS
        for col in cols:
            if col not in out.columns:
                out[col] = pd.NA
        now = pd.Timestamp.now()
        out["ts_code"] = out["ts_code"].astype(str)
        out["name"] = out["name"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["industry_source"] = out["industry_source"].fillna(out["source"]).astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["industry_name"] = out["industry_name"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["sw_code_2021"] = out["sw_code_2021"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        for col in ("industry_code_l1", "industry_name_l1", "industry_code_l2", "industry_name_l2", "industry_code_l3", "industry_name_l3"):
            out[col] = out[col].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["source"] = out["source"].fillna(out["industry_source"]).astype(str)
        out["effective_date"] = pd.to_datetime(out["effective_date"], errors="coerce").dt.date
        out["is_current"] = out["is_current"].fillna(True).astype(bool)
        out["created_at"] = pd.to_datetime(out["created_at"], errors="coerce").fillna(now)
        out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce").fillna(now)
        out["fetched_at"] = pd.to_datetime(out["fetched_at"], errors="coerce").fillna(out["updated_at"])
        out = out.dropna(subset=["ts_code"])
        out = out.sort_values(["ts_code", "is_current", "effective_date", "updated_at"], ascending=[True, False, False, False]).drop_duplicates(["ts_code", "industry_source", "effective_date"], keep="first")
        conn = self.connect()
        temp_name = f"tmp_stock_industry_map_{uuid.uuid4().hex[:8]}"
        conn.register(temp_name, out[cols])
        try:
            conn.execute(
                f"""
                UPDATE stock_industry_map
                SET is_current = FALSE, updated_at = NOW()
                WHERE ts_code IN (SELECT ts_code FROM {temp_name} WHERE is_current)
                  AND industry_source IN (SELECT industry_source FROM {temp_name} WHERE is_current)
                """
            )
            conn.execute(
                f"""
                DELETE FROM stock_industry_map AS t
                USING {temp_name} AS s
                WHERE t.ts_code = s.ts_code
                  AND COALESCE(t.industry_source, '') = COALESCE(s.industry_source, '')
                  AND COALESCE(t.effective_date, DATE '1900-01-01') = COALESCE(s.effective_date, DATE '1900-01-01')
                """
            )
            conn.execute(f"INSERT INTO stock_industry_map ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {temp_name}")
            self._write_stock_industry_map_lake(out[cols])
            self.refresh_views()
        finally:
            conn.unregister(temp_name)
        return len(out)

    def _write_stock_industry_map_lake(self, df: pd.DataFrame) -> int:
        rows_written = 0
        out = df.copy()
        out["effective_date"] = pd.to_datetime(out["effective_date"], errors="coerce").dt.date
        out["partition_effective_date"] = out["effective_date"].map(lambda value: "unknown" if pd.isna(value) else str(value))
        for effective_date, part_df in out.groupby("partition_effective_date", dropna=False):
            rel = Path("stock_industry_map") / f"effective_date={effective_date}"
            merged = self._merge_partition(
                rel,
                part_df.drop(columns=["partition_effective_date"]),
                ["ts_code", "industry_source", "effective_date"],
                STOCK_INDUSTRY_MAP_COLUMNS,
            )
            rows_written += len(merged)
        self.rebuild_manifest("stock_industry_map")
        return rows_written

    def write_industry_board_local(self, df: pd.DataFrame, job_id: str | None = None) -> int:
        if df is None or df.empty:
            return 0
        job_id = job_id or self._start_job("industry_board_local", {})
        out = self._prepare_industry_board_local(df)
        rows_written = 0
        try:
            for (year, month), part_df in out.groupby(["year", "month"], dropna=False):
                rel = Path("industry_board_local") / f"year={year}" / f"month={month}"
                merged = self._merge_partition(
                    rel,
                    part_df,
                    ["trade_date", "industry_source", "industry_level", "industry_code"],
                    INDUSTRY_BOARD_LOCAL_COLUMNS,
                )
                rows_written += len(merged)
                self._record_ingestion(job_id, "industry_board_local", rel, len(part_df), len(merged), "OK", "")
            self.rebuild_manifest("industry_board_local")
            self.refresh_views()
            self._finish_job(job_id, "OK", f"rows_in={len(df)} rows_written={rows_written}")
            return len(out)
        except Exception as exc:
            self._finish_job(job_id, "FAIL", repr(exc))
            raise

    def write_industry_validation(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        out = df.copy()
        now = pd.Timestamp.now()
        for col in INDUSTRY_VALIDATION_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out["validated_at"] = pd.to_datetime(out["validated_at"], errors="coerce").fillna(now)
        out["created_at"] = pd.to_datetime(out["created_at"], errors="coerce").fillna(now)
        out["year"] = out["validated_at"].dt.strftime("%Y")
        out["month"] = out["validated_at"].dt.strftime("%m")
        rows_written = 0
        for (year, month), part_df in out.groupby(["year", "month"], dropna=False):
            rel = Path("industry_board_validation") / f"year={year}" / f"month={month}"
            merged = self._merge_partition(rel, part_df, ["validation_id", "check_name"], INDUSTRY_VALIDATION_COLUMNS)
            rows_written += len(merged)
        self.refresh_views()
        return rows_written

    def write_sw_industry_index_daily(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        out = df.copy()
        now = pd.Timestamp.now()
        for col in SW_INDUSTRY_INDEX_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["created_at"] = pd.to_datetime(out["created_at"], errors="coerce").fillna(now)
        out = out.dropna(subset=["trade_date", "industry_name"])
        out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
        out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        rows_written = 0
        for (year, month), part_df in out.groupby(["year", "month"], dropna=False):
            rel = Path("sw_industry_index_daily") / f"year={year}" / f"month={month}"
            merged = self._merge_partition(rel, part_df, ["trade_date", "industry_code", "industry_name"], SW_INDUSTRY_INDEX_COLUMNS)
            rows_written += len(merged)
        self.refresh_views()
        return rows_written

    def upsert_daily_price(self, df: pd.DataFrame, adjust_type: str = "qfq", job_id: str | None = None) -> int:
        if df is None or df.empty:
            return 0
        job_id = job_id or self._start_job("daily_price", {"adjust_type": adjust_type})
        out = self._prepare_daily_price(df, adjust_type)
        rows_written = 0
        try:
            for (part_adjust, year, month), part_df in out.groupby(["adjust_type", "year", "month"], dropna=False):
                rel = Path("daily_price") / f"adjust_type={part_adjust}" / f"year={year}" / f"month={month}"
                merged = self._merge_partition(rel, part_df, ["ts_code", "trade_date", "adjust_type"], DAILY_PRICE_COLUMNS)
                rows_written += len(merged)
                self._record_ingestion(job_id, "daily_price", rel, len(part_df), len(merged), "OK", "")
            self.rebuild_manifest("daily_price")
            self.refresh_views()
            self._finish_job(job_id, "OK", f"rows_in={len(df)} rows_written={rows_written}")
            return len(df)
        except Exception as exc:
            self._finish_job(job_id, "FAIL", repr(exc))
            raise

    def dedupe_daily_price_partitions(self) -> dict[str, int]:
        """Rewrite existing daily_price partitions so raw parquet matches the primary key contract."""
        stats = {"partitions_scanned": 0, "partitions_rewritten": 0, "rows_before": 0, "rows_after": 0}
        for partition in self._partition_dirs("daily_price"):
            rel = partition.relative_to(self.lake_root)
            df = self._read_partition(rel)
            if df.empty:
                continue
            stats["partitions_scanned"] += 1
            before = len(df)
            for col in DAILY_PRICE_COLUMNS:
                if col not in df.columns:
                    df[col] = pd.NA
            deduped = self._dedupe(df[DAILY_PRICE_COLUMNS], ["ts_code", "trade_date", "adjust_type"])
            stats["rows_before"] += before
            stats["rows_after"] += len(deduped)
            if len(deduped) != before:
                self._write_partition(rel, deduped[DAILY_PRICE_COLUMNS])
                stats["partitions_rewritten"] += 1
        if stats["partitions_rewritten"]:
            self.rebuild_manifest("daily_price")
            self.refresh_views()
        return stats

    def upsert_index_daily(self, df: pd.DataFrame, job_id: str | None = None) -> int:
        if df is None or df.empty:
            return 0
        job_id = job_id or self._start_job("index_daily", {})
        out = self._prepare_index_daily(df)
        rows_written = 0
        try:
            for (year, month), part_df in out.groupby(["year", "month"], dropna=False):
                rel = Path("index_daily") / f"year={year}" / f"month={month}"
                merged = self._merge_partition(rel, part_df, ["index_code", "trade_date"], INDEX_DAILY_COLUMNS)
                rows_written += len(merged)
                self._record_ingestion(job_id, "index_daily", rel, len(part_df), len(merged), "OK", "")
            self.rebuild_manifest("index_daily")
            self.refresh_views()
            self._finish_job(job_id, "OK", f"rows_in={len(df)} rows_written={rows_written}")
            return len(df)
        except Exception as exc:
            self._finish_job(job_id, "FAIL", repr(exc))
            raise

    def write_stock_basic_snapshot(self, df: pd.DataFrame, snapshot_date: str | None = None, job_id: str | None = None) -> int:
        if df is None or df.empty:
            return 0
        snapshot_date = snapshot_date or pd.Timestamp.now().strftime("%Y-%m-%d")
        job_id = job_id or self._start_job("stock_basic", {"snapshot_date": snapshot_date})
        out = df.copy()
        out["snapshot_date"] = pd.to_datetime(snapshot_date).date()
        out["source"] = out["source"] if "source" in out.columns else "akshare.stock_info_a_code_name"
        out = self._with_source_priority(out)
        for col in STOCK_BASIC_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce").fillna(pd.Timestamp.now())
        out = self._dedupe(out[STOCK_BASIC_COLUMNS], ["ts_code", "snapshot_date"])
        rel = Path("stock_basic") / f"snapshot_date={snapshot_date}"
        try:
            self._write_partition(rel, out)
            self._record_ingestion(job_id, "stock_basic", rel, len(df), len(out), "OK", "")
            self.rebuild_manifest("stock_basic")
            self.refresh_views()
            self._finish_job(job_id, "OK", f"rows_in={len(df)} rows_written={len(out)}")
            return len(df)
        except Exception as exc:
            self._finish_job(job_id, "FAIL", repr(exc))
            raise

    def write_industry_board_snapshot(self, df: pd.DataFrame, snapshot_date: str | None = None, job_id: str | None = None) -> int:
        if df is None or df.empty:
            return 0
        snapshot_date = snapshot_date or pd.Timestamp.now().strftime("%Y-%m-%d")
        job_id = job_id or self._start_job("industry_board", {"snapshot_date": snapshot_date})
        out = self._prepare_industry_board(df, snapshot_date)
        rel = Path("industry_board") / f"snapshot_date={snapshot_date}"
        try:
            self._write_partition(rel, out[INDUSTRY_BOARD_COLUMNS])
            self._record_ingestion(job_id, "industry_board", rel, len(df), len(out), "OK", "")
            self.rebuild_manifest("industry_board")
            self.refresh_views()
            self._finish_job(job_id, "OK", f"rows_in={len(df)} rows_written={len(out)}")
            return len(df)
        except Exception as exc:
            self._finish_job(job_id, "FAIL", repr(exc))
            raise

    def rebuild_manifest(self, dataset_name: str | None = None) -> None:
        conn = self.connect()
        datasets = [dataset_name] if dataset_name else ["daily_price", "index_daily", "stock_basic", "stock_industry_map", "industry_board", "industry_board_local", "sw_industry_index_daily", "industry_board_validation"]
        for dataset in datasets:
            conn.execute("DELETE FROM dataset_manifest WHERE dataset_name = ?", [dataset])
            rows = []
            for partition in self._partition_dirs(dataset):
                files = sorted(partition.glob("*.parquet"))
                if not files:
                    continue
                df = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
                rel = partition.relative_to(self.lake_root)
                spec = self._partition_spec(rel)
                rows.append(
                    {
                        "dataset_name": dataset,
                        "partition_path": rel.as_posix(),
                        "partition_spec": json.dumps(spec, ensure_ascii=False, sort_keys=True),
                        "row_count": len(df),
                        "file_count": len(files),
                        "min_trade_date": self._min_date(df, "trade_date"),
                        "max_trade_date": self._max_date(df, "trade_date"),
                        "snapshot_date": self._min_date(df, "snapshot_date"),
                        "content_hash": self._content_hash(files),
                        "updated_at": pd.Timestamp.now(),
                    }
                )
            if rows:
                conn.register("tmp_manifest", pd.DataFrame(rows))
                try:
                    conn.execute("INSERT INTO dataset_manifest SELECT * FROM tmp_manifest")
                finally:
                    conn.unregister("tmp_manifest")
        self._rebuild_indexes()

    def refresh_views(self) -> None:
        conn = self.connect()
        conn.execute(self._daily_price_view_sql())
        conn.execute(self._view_sql("v_index_daily", "index_daily", INDEX_DAILY_COLUMNS))
        conn.execute(self._stock_industry_map_sql())
        conn.execute(self._stock_basic_latest_sql())
        conn.execute(self._industry_board_latest_sql())
        conn.execute(self._industry_board_local_sql())
        conn.execute(self._sw_industry_index_sql())
        conn.execute(self._industry_validation_sql())
        conn.execute(self._industry_board_sql())
        for name in ("daily_price", "index_daily", "stock_basic", "industry_board"):
            if not self._table_or_view_exists(name):
                latest_view = name in {"stock_basic", "industry_board"}
                conn.execute(f"CREATE VIEW {name} AS SELECT * FROM v_{name}_latest" if latest_view else f"CREATE VIEW {name} AS SELECT * FROM v_{name}")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _merge_partition(self, rel: Path, part_df: pd.DataFrame, keys: list[str], columns: list[str]) -> pd.DataFrame:
        existing = self._read_partition(rel)
        merged = pd.concat([existing, part_df[columns]], ignore_index=True) if not existing.empty else part_df[columns].copy()
        merged = self._dedupe(merged, keys)
        self._write_partition(rel, merged[columns])
        return merged

    def _write_partition(self, rel: Path, df: pd.DataFrame) -> Path:
        partition_dir = self.lake_root / rel
        if partition_dir.exists():
            self._backup_partition(rel)
            shutil.rmtree(partition_dir)
        partition_dir.mkdir(parents=True, exist_ok=True)
        path = partition_dir / f"part-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}.parquet"
        df.to_parquet(path, index=False)
        return path

    def _backup_partition(self, rel: Path) -> None:
        src = self.lake_root / rel
        if not src.exists() or self._backup_disabled():
            return
        # 硬链接快照：同文件系统内 O(1)，备份目录持有旧文件 inode，
        # 主目录重写后旧版本数据依然完整保留，避免每次全量复制占空间。
        dest = self.backup_root / f"{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, copy_function=os.link)
        self._prune_backups()

    @staticmethod
    def _backup_disabled() -> bool:
        return int(os.environ.get("LAKE_BACKUP_KEEP", "1")) <= 0

    def _prune_backups(self, keep: int | None = None) -> None:
        """备份保留策略：每个分区只保留最近 N 份快照（默认 1 份），防止 backups/ 膨胀。

        用 LAKE_BACKUP_KEEP=0 可完全关闭备份（_backup_partition 直接跳过）。
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

    def _read_partition(self, rel: Path) -> pd.DataFrame:
        partition_dir = self.lake_root / rel
        files = sorted(partition_dir.glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)

    def _prepare_daily_price(self, df: pd.DataFrame, adjust_type: str) -> pd.DataFrame:
        out = df.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        if "adjust_type" in out.columns:
            out["adjust_type"] = out["adjust_type"].fillna(adjust_type)
        else:
            out["adjust_type"] = adjust_type
        out["source"] = out["source"] if "source" in out.columns else ""
        out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce").fillna(pd.Timestamp.now()) if "updated_at" in out.columns else pd.Timestamp.now()
        out = self._with_source_priority(out)
        for col in DAILY_PRICE_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out = out.dropna(subset=["ts_code", "trade_date", "adjust_type"])
        out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
        out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        return self._dedupe(out, ["ts_code", "trade_date", "adjust_type"])

    def _prepare_index_daily(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["source"] = out["source"] if "source" in out.columns else ""
        out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce").fillna(pd.Timestamp.now()) if "updated_at" in out.columns else pd.Timestamp.now()
        out = self._with_source_priority(out)
        for col in INDEX_DAILY_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out = out.dropna(subset=["index_code", "trade_date"])
        out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
        out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        return self._dedupe(out, ["index_code", "trade_date"])

    def _prepare_industry_board(self, df: pd.DataFrame, snapshot_date: str) -> pd.DataFrame:
        rename_map = {
            "板块代码": "board_code",
            "代码": "board_code",
            "板块名称": "board_name",
            "名称": "board_name",
            "最新价": "latest_price",
            "涨跌额": "change_amount",
            "涨跌幅": "pct_chg",
            "总市值": "total_market_value",
            "换手率": "turnover_rate",
            "上涨家数": "rising_count",
            "下跌家数": "falling_count",
            "领涨股票": "leading_stock",
            "领涨股票-涨跌幅": "leading_stock_pct_chg",
        }
        out = df.rename(columns={col: rename_map[col] for col in df.columns if col in rename_map}).copy()
        out["snapshot_date"] = pd.to_datetime(snapshot_date).date()
        out["board_type"] = out["board_type"] if "board_type" in out.columns else "industry"
        out["source"] = out["source"] if "source" in out.columns else "akshare.stock_board_industry_name_em"
        out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce").fillna(pd.Timestamp.now()) if "updated_at" in out.columns else pd.Timestamp.now()
        for col in INDUSTRY_BOARD_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        for col in ("latest_price", "change_amount", "pct_chg", "total_market_value", "turnover_rate", "leading_stock_pct_chg"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        for col in ("rising_count", "falling_count"):
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
        out["board_code"] = out["board_code"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["board_name"] = out["board_name"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out = out.dropna(subset=["board_name"])
        out = self._with_source_priority(out)
        return self._dedupe(out[INDUSTRY_BOARD_COLUMNS], ["board_type", "board_name", "snapshot_date"])

    def _prepare_industry_board_local(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        now = pd.Timestamp.now()
        for col in INDUSTRY_BOARD_LOCAL_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        out["source"] = out["source"].fillna("local_aggregate").astype(str)
        out["created_at"] = pd.to_datetime(out["created_at"], errors="coerce").fillna(now)
        for col in ("member_count", "valid_member_count", "unmapped_count", "up_count", "down_count", "flat_count"):
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
        for col in (
            "avg_ret_1d",
            "median_ret_1d",
            "avg_ret_5d",
            "median_ret_5d",
            "avg_ret_20d",
            "median_ret_20d",
            "amount_sum",
            "volume_sum",
            "amount_ma20",
            "breadth_up_ratio",
            "industry_momentum_score",
            "industry_breadth_score",
            "industry_liquidity_score",
            "industry_strength_score",
        ):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out["industry_source"] = out["industry_source"].fillna("baostock").astype(str)
        out["industry_level"] = out["industry_level"].fillna("l1").astype(str)
        out["industry_code"] = out["industry_code"].fillna(out["industry_name"]).astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out["industry_name"] = out["industry_name"].astype(str).replace({"<NA>": pd.NA, "nan": pd.NA, "None": pd.NA})
        out = out.dropna(subset=["trade_date", "industry_name"])
        out["year"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y")
        out["month"] = pd.to_datetime(out["trade_date"]).dt.strftime("%m")
        return out[INDUSTRY_BOARD_LOCAL_COLUMNS + ["year", "month"]]

    def _with_source_priority(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "source_priority" not in out.columns:
            out["source_priority"] = out["source"].map(self._source_priority)
        out["source_priority"] = pd.to_numeric(out["source_priority"], errors="coerce").fillna(0).astype("int64")
        return out

    @staticmethod
    def _source_priority(source: Any) -> int:
        source_text = str(source).lower()
        for token, priority in SOURCE_PRIORITY.items():
            if token in source_text:
                return priority
        return 0

    @staticmethod
    def _dedupe(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        for key in keys:
            if key.endswith("_date") and key in out.columns:
                out[key] = pd.to_datetime(out[key], errors="coerce").dt.date
        sort_cols = list(keys)
        ascending = [True] * len(keys)
        if "source_priority" in out.columns:
            out["source_priority"] = pd.to_numeric(out["source_priority"], errors="coerce").fillna(0).astype("int64")
            sort_cols.append("source_priority")
            ascending.append(False)
        for time_col in ("updated_at", "created_at", "validated_at", "fetched_at"):
            if time_col in out.columns:
                out[time_col] = pd.to_datetime(out[time_col], errors="coerce").fillna(pd.Timestamp.min)
                sort_cols.append(time_col)
                ascending.append(False)
                break
        out = out.sort_values(sort_cols, ascending=ascending)
        return out.drop_duplicates(keys, keep="first").reset_index(drop=True)

    def _partition_dirs(self, dataset: str) -> list[Path]:
        base = self.lake_root / dataset
        if not base.exists():
            return []
        return sorted({path.parent for path in base.rglob("*.parquet")})

    @staticmethod
    def _partition_spec(rel: Path) -> dict[str, str]:
        spec: dict[str, str] = {}
        for part in rel.parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                spec[key] = value
        return spec

    @staticmethod
    def _min_date(df: pd.DataFrame, col: str):
        if col not in df.columns or df.empty:
            return None
        value = pd.to_datetime(df[col], errors="coerce").min()
        return None if pd.isna(value) else value.date()

    @staticmethod
    def _max_date(df: pd.DataFrame, col: str):
        if col not in df.columns or df.empty:
            return None
        value = pd.to_datetime(df[col], errors="coerce").max()
        return None if pd.isna(value) else value.date()

    @staticmethod
    def _content_hash(files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.name.encode("utf-8"))
            digest.update(str(path.stat().st_size).encode("utf-8"))
            digest.update(str(int(path.stat().st_mtime_ns)).encode("utf-8"))
        return digest.hexdigest()

    def _rebuild_indexes(self) -> None:
        conn = self.connect()
        for table in ("symbol_coverage", "unique_key_index"):
            conn.execute(f"DELETE FROM {table}")
        self.refresh_views()
        if self._dataset_has_files("daily_price"):
            conn.execute(
                """
                INSERT INTO symbol_coverage
                SELECT 'daily_price', ts_code, adjust_type, MIN(trade_date), MAX(trade_date), COUNT(*), NOW()
                FROM v_daily_price
                GROUP BY ts_code, adjust_type
                """
            )
            conn.execute(
                """
                INSERT INTO unique_key_index
                SELECT 'daily_price',
                       ts_code || '|' || CAST(trade_date AS VARCHAR) || '|' || adjust_type,
                       'daily_price/adjust_type=' || adjust_type || '/year=' || STRFTIME(trade_date, '%Y') || '/month=' || STRFTIME(trade_date, '%m'),
                       source, source_priority, updated_at
                FROM v_daily_price
                """
            )
        if self._dataset_has_files("index_daily"):
            conn.execute(
                """
                INSERT INTO symbol_coverage
                SELECT 'index_daily', index_code, NULL, MIN(trade_date), MAX(trade_date), COUNT(*), NOW()
                FROM v_index_daily
                GROUP BY index_code
                """
            )
            conn.execute(
                """
                INSERT INTO unique_key_index
                SELECT 'index_daily',
                       index_code || '|' || CAST(trade_date AS VARCHAR),
                       'index_daily/year=' || STRFTIME(trade_date, '%Y') || '/month=' || STRFTIME(trade_date, '%m'),
                       source, source_priority, updated_at
                FROM v_index_daily
                """
            )

    def _view_sql(self, view_name: str, dataset: str, columns: list[str]) -> str:
        if self._dataset_has_files(dataset):
            glob = (self.lake_root / dataset / "**" / "*.parquet").as_posix().replace("'", "''")
            return f"CREATE OR REPLACE VIEW {view_name} AS SELECT {', '.join(columns)} FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)"
        return f"CREATE OR REPLACE VIEW {view_name} AS {self._empty_select(columns)}"

    def _daily_price_view_sql(self) -> str:
        """v_daily_price: dedup per (ts_code, trade_date) across sources & adjust_types."""
        if not self._dataset_has_files("daily_price"):
            return f"CREATE OR REPLACE VIEW v_daily_price AS {self._empty_select(DAILY_PRICE_COLUMNS)}"
        glob = (self.lake_root / "daily_price" / "**" / "*.parquet").as_posix().replace("'", "''")
        cols = ", ".join(DAILY_PRICE_COLUMNS)
        return f"""
            CREATE OR REPLACE VIEW v_daily_price AS
            SELECT {cols}
            FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY ts_code, trade_date
                ORDER BY source_priority DESC,
                         CASE adjust_type WHEN 'qfq' THEN 1 WHEN '' THEN 2 ELSE 3 END,
                         updated_at DESC
            ) = 1
        """

    def _stock_basic_latest_sql(self) -> str:
        if not self._dataset_has_files("stock_basic"):
            return f"CREATE OR REPLACE VIEW v_stock_basic_latest AS {self._empty_select(STOCK_BASIC_COLUMNS)}"
        glob = (self.lake_root / "stock_basic" / "**" / "*.parquet").as_posix().replace("'", "''")
        return f"""
            CREATE OR REPLACE VIEW v_stock_basic_latest AS
            SELECT {', '.join(STOCK_BASIC_COLUMNS)}
            FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY snapshot_date DESC, source_priority DESC, updated_at DESC) = 1
        """

    def _stock_industry_map_sql(self) -> str:
        cols = ", ".join(STOCK_INDUSTRY_MAP_COLUMNS)
        if self._dataset_has_files("stock_industry_map"):
            glob = (self.lake_root / "stock_industry_map" / "**" / "*.parquet").as_posix().replace("'", "''")
            return f"""
                CREATE OR REPLACE VIEW v_stock_industry_map AS
                SELECT {cols}
                FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY ts_code
                    ORDER BY is_current DESC NULLS LAST, effective_date DESC NULLS LAST, updated_at DESC NULLS LAST, fetched_at DESC NULLS LAST
                ) = 1
            """
        return f"""
            CREATE OR REPLACE VIEW v_stock_industry_map AS
            SELECT {cols}
            FROM stock_industry_map
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY ts_code
                ORDER BY is_current DESC NULLS LAST, effective_date DESC NULLS LAST, updated_at DESC NULLS LAST, fetched_at DESC NULLS LAST
            ) = 1
        """

    def _industry_board_latest_sql(self) -> str:
        if not self._dataset_has_files("industry_board"):
            return f"CREATE OR REPLACE VIEW v_industry_board_latest AS {self._empty_select(INDUSTRY_BOARD_COLUMNS)}"
        glob = (self.lake_root / "industry_board" / "**" / "*.parquet").as_posix().replace("'", "''")
        return f"""
            CREATE OR REPLACE VIEW v_industry_board_latest AS
            SELECT {', '.join(INDUSTRY_BOARD_COLUMNS)}
            FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY board_type, board_name ORDER BY snapshot_date DESC, source_priority DESC, updated_at DESC) = 1
        """

    def _industry_board_local_sql(self) -> str:
        return self._view_sql("v_industry_board_local", "industry_board_local", INDUSTRY_BOARD_LOCAL_COLUMNS)

    def _sw_industry_index_sql(self) -> str:
        return self._view_sql("v_sw_industry_index_daily", "sw_industry_index_daily", SW_INDUSTRY_INDEX_COLUMNS)

    def _industry_validation_sql(self) -> str:
        return self._view_sql("v_industry_board_validation", "industry_board_validation", INDUSTRY_VALIDATION_COLUMNS)

    def _industry_board_sql(self) -> str:
        if not self._dataset_has_files("industry_board_local"):
            return f"CREATE OR REPLACE VIEW v_industry_board AS {self._empty_select(INDUSTRY_BOARD_VIEW_COLUMNS)}"
        return f"""
            CREATE OR REPLACE VIEW v_industry_board AS
            SELECT {', '.join(INDUSTRY_BOARD_VIEW_COLUMNS)}
            FROM v_industry_board_local
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY trade_date, industry_source, industry_level, industry_code
                ORDER BY created_at DESC NULLS LAST
            ) = 1
        """

    @staticmethod
    def _empty_select(columns: list[str]) -> str:
        expressions = []
        for col in columns:
            if col in {"trade_date", "snapshot_date", "list_date"}:
                expressions.append(f"CAST(NULL AS DATE) AS {col}")
            elif col in {"open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "turn", "latest_price", "change_amount", "total_market_value", "turnover_rate", "leading_stock_pct_chg", "avg_ret_1d", "median_ret_1d", "avg_ret_5d", "median_ret_5d", "avg_ret_20d", "median_ret_20d", "amount_sum", "volume_sum", "amount_ma20", "breadth_up_ratio", "industry_momentum_score", "industry_breadth_score", "industry_liquidity_score", "industry_strength_score"}:
                expressions.append(f"CAST(NULL AS DOUBLE) AS {col}")
            elif col in {"source_priority", "rising_count", "falling_count", "member_count", "valid_member_count", "unmapped_count", "up_count", "down_count", "flat_count"}:
                expressions.append(f"CAST(NULL AS INTEGER) AS {col}")
            elif col in {"updated_at", "created_at", "validated_at", "fetched_at"}:
                expressions.append(f"CAST(NULL AS TIMESTAMP) AS {col}")
            elif col == "is_current":
                expressions.append(f"CAST(NULL AS BOOLEAN) AS {col}")
            else:
                expressions.append(f"CAST(NULL AS VARCHAR) AS {col}")
        return f"SELECT {', '.join(expressions)} WHERE FALSE"

    def _dataset_has_files(self, dataset: str) -> bool:
        return any((self.lake_root / dataset).rglob("*.parquet")) if (self.lake_root / dataset).exists() else False

    def _table_or_view_exists(self, name: str) -> bool:
        row = self.connect().execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()
        return bool(row and row[0])

    def _start_job(self, dataset_name: str, parameters: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        self.execute(
            "INSERT INTO ingestion_job VALUES (?, ?, ?, ?, ?, ?, ?)",
            [job_id, dataset_name, pd.Timestamp.now(), None, "RUNNING", json.dumps(parameters, ensure_ascii=False), ""],
        )
        return job_id

    def _finish_job(self, job_id: str, status: str, message: str) -> None:
        self.execute(
            "UPDATE ingestion_job SET finished_at = ?, status = ?, message = ? WHERE job_id = ?",
            [pd.Timestamp.now(), status, message, job_id],
        )

    def _record_ingestion(self, job_id: str, dataset_name: str, rel: Path, records_in: int, records_written: int, status: str, message: str) -> None:
        self.execute(
            "INSERT INTO ingestion_record VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [job_id, dataset_name, rel.as_posix(), records_in, records_written, status, message, pd.Timestamp.now()],
        )
