from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from src.utils.config import ENV_DB_PATH, load_settings, resolve_db_path as resolve_config_db_path


class StockDataLoader:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = self.resolve_db_path(db_path)
        self.conn = duckdb.connect(str(self.db_path), read_only=True)
        self.tables_used: dict[str, str] = {}
        self.lake_root = self._resolve_lake_root(self.db_path)

    @staticmethod
    def resolve_db_path(db_path: str | Path | None = None) -> Path:
        requested = Path(db_path).expanduser().resolve(strict=False) if db_path else None
        if requested and requested.exists():
            return requested
        env_db = os.environ.get(ENV_DB_PATH)
        env_path = Path(env_db).expanduser().resolve(strict=False) if env_db else None
        if not requested and env_path and env_path.exists():
            return env_path
        settings = load_settings()
        configured = resolve_config_db_path(settings)
        if not requested and configured.exists():
            return configured
        if requested:
            root = requested.parent.parent if requested.parent.name == "db" else requested.parent
        else:
            root = configured.parent
        candidates = []
        if root.exists():
            candidates.extend(root.rglob("*.duckdb"))
        configured_root = configured.parent
        if configured_root.exists() and configured_root != root:
            candidates.extend(configured_root.rglob("*.duckdb"))
        preferred = sorted(set(candidates), key=lambda p: (p.name != "stock_data_v2.duckdb", p.name != "metadata.duckdb", p.name))
        for candidate in preferred:
            if StockDataLoader._db_has_readable_market_data(candidate) or StockDataLoader._lake_has_market_data(StockDataLoader._resolve_lake_root(candidate)):
                return candidate
        if requested:
            raise FileNotFoundError(f"DuckDB file not found and no readable fallback discovered: {requested}")
        raise FileNotFoundError("No readable DuckDB database found.")

    @staticmethod
    def _db_has_readable_market_data(path: Path) -> bool:
        try:
            con = duckdb.connect(str(path), read_only=True)
            try:
                for name in ("v_daily_price", "daily_price"):
                    try:
                        con.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
                        return True
                    except Exception:
                        continue
            finally:
                con.close()
        except Exception:
            return False
        return False

    @staticmethod
    def _resolve_lake_root(db_path: Path) -> Path:
        if db_path.parent.name == "db":
            return (db_path.parent.parent / "lake").expanduser().resolve(strict=False)
        return (db_path.parent / "lake").expanduser().resolve(strict=False)

    @staticmethod
    def _lake_has_market_data(lake_root: Path) -> bool:
        return any((lake_root / "daily_price").rglob("*.parquet")) if lake_root.exists() else False

    def close(self) -> None:
        self.conn.close()

    def table_or_view_exists(self, name: str) -> bool:
        try:
            self.conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
            return True
        except Exception:
            return False

    def resolve_table(self, preferred_view: str, fallback_table: str) -> str:
        for name in (preferred_view, fallback_table):
            if self.table_or_view_exists(name):
                self.tables_used[fallback_table] = name
                return name
        parquet_relation = self._parquet_relation(fallback_table)
        if parquet_relation:
            self.tables_used[fallback_table] = f"lake/{fallback_table} parquet"
            return parquet_relation
        raise RuntimeError(f"Neither {preferred_view} nor {fallback_table} is readable in {self.db_path}")

    def get_stock_price(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust_type: str = "qfq",
    ) -> pd.DataFrame:
        table = self.resolve_table("v_daily_price", "daily_price")
        where = ["ts_code = ?"]
        params: list[object] = [ts_code]
        if self._has_column(table, "adjust_type"):
            where.append("(adjust_type = ? OR adjust_type IS NULL)")
            params.append(adjust_type)
        self._append_date_filter(where, params, start_date, end_date)
        sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)}"
        sql = self._dedupe_daily_sql(sql, table)
        return self._query(f"{sql} ORDER BY trade_date", params)

    def get_index_price(
        self,
        index_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        table = self.resolve_table("v_index_daily", "index_daily")
        where = ["index_code = ?"]
        params: list[object] = [index_code]
        self._append_date_filter(where, params, start_date, end_date)
        return self._query(f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY trade_date", params)

    def get_stock_basic(self) -> pd.DataFrame:
        table = self.resolve_table("v_stock_basic_latest", "stock_basic")
        industry_map = self._stock_industry_relation()
        if industry_map:
            return self._query(
                f"""
                SELECT
                    b.* EXCLUDE(industry),
                    b.industry AS stock_basic_industry,
                    COALESCE(m.industry_name, b.industry) AS industry,
                    m.sw_code_2021,
                    COALESCE(m.industry_source, m.source) AS industry_source,
                    COALESCE(m.updated_at, m.fetched_at) AS industry_fetched_at
                FROM {table} AS b
                LEFT JOIN {industry_map} AS m USING(ts_code)
                ORDER BY ts_code
                """,
                [],
            )
        return self._query(f"SELECT * FROM {table} ORDER BY ts_code", [])

    def get_latest_trade_date(self) -> str:
        table = self.resolve_table("v_daily_price", "daily_price")
        row = self.conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
        if not row or row[0] is None:
            raise RuntimeError(f"No trade_date found in {table}")
        return str(row[0])

    def get_market_price(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust_type: str = "qfq",
    ) -> pd.DataFrame:
        table = self.resolve_table("v_daily_price", "daily_price")
        where: list[str] = []
        params: list[object] = []
        if self._has_column(table, "adjust_type"):
            where.append("(adjust_type = ? OR adjust_type IS NULL)")
            params.append(adjust_type)
        self._append_date_filter(where, params, start_date, end_date)
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {' AND '.join(where)}"
        sql = self._dedupe_daily_sql(sql, table)
        sql += " ORDER BY ts_code, trade_date"
        return self._query(sql, params)

    def table_counts(self) -> dict[str, int]:
        counts = {}
        for view, table in (
            ("v_daily_price", "daily_price"),
            ("v_index_daily", "index_daily"),
            ("v_stock_basic_latest", "stock_basic"),
        ):
            try:
                name = self.resolve_table(view, table)
                counts[name] = int(self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            except Exception:
                counts[table] = 0
        return counts

    def _has_column(self, table: str, column: str) -> bool:
        try:
            rows = self.conn.execute(f"DESCRIBE SELECT * FROM {table} LIMIT 0").fetchall()
            return column in {row[0] for row in rows}
        except Exception:
            return False

    def _dedupe_daily_sql(self, sql: str, table: str) -> str:
        required = {"ts_code", "trade_date"}
        if not all(self._has_column(table, col) for col in required):
            return sql
        order_terms = []
        if self._has_column(table, "source_priority"):
            order_terms.append("source_priority DESC NULLS LAST")
        if self._has_column(table, "updated_at"):
            order_terms.append("updated_at DESC NULLS LAST")
        order_sql = ", ".join(order_terms) if order_terms else "trade_date DESC"
        partition_cols = "ts_code, trade_date"
        if self._has_column(table, "adjust_type"):
            partition_cols += ", adjust_type"
        return (
            f"SELECT * FROM ({sql}) "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition_cols} ORDER BY {order_sql}) = 1"
        )

    def _parquet_relation(self, dataset: str) -> str | None:
        dataset_root = self.lake_root / dataset
        if not dataset_root.exists() or not any(dataset_root.rglob("*.parquet")):
            return None
        glob = (dataset_root / "**" / "*.parquet").as_posix().replace("'", "''")
        if dataset == "stock_basic":
            return (
                "(SELECT * FROM read_parquet('"
                + glob
                + "', hive_partitioning=true) "
                + "QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY COALESCE(snapshot_date, updated_at) DESC) = 1)"
            )
        return f"(SELECT * FROM read_parquet('{glob}', hive_partitioning=true))"

    def _stock_industry_relation(self) -> str | None:
        if self.table_or_view_exists("v_stock_industry_map"):
            return (
                "(SELECT ts_code, industry_name, sw_code_2021, industry_source, source, updated_at, fetched_at "
                "FROM v_stock_industry_map)"
            )
        if self.table_or_view_exists("stock_industry_map"):
            cols = {row[0] for row in self.conn.execute("DESCRIBE SELECT * FROM stock_industry_map LIMIT 0").fetchall()}
            industry_source_expr = "industry_source" if "industry_source" in cols else "source AS industry_source"
            updated_expr = "updated_at" if "updated_at" in cols else "fetched_at AS updated_at"
            is_current_order = "is_current DESC NULLS LAST," if "is_current" in cols else ""
            effective_order = "effective_date DESC NULLS LAST," if "effective_date" in cols else ""
            updated_order = "updated_at DESC NULLS LAST," if "updated_at" in cols else ""
            return (
                f"(SELECT ts_code, industry_name, sw_code_2021, {industry_source_expr}, source, {updated_expr}, fetched_at "
                "FROM stock_industry_map "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY {is_current_order} {effective_order} {updated_order} fetched_at DESC NULLS LAST) = 1)"
            )
        return None

    @staticmethod
    def _append_date_filter(where: list[str], params: list[object], start_date: str | None, end_date: str | None) -> None:
        if start_date:
            where.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            where.append("trade_date <= ?")
            params.append(end_date)

    def _query(self, sql: str, params: Iterable[object]) -> pd.DataFrame:
        return self.conn.execute(sql, list(params)).df()
