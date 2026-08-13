from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from src.utils.config import resolve_path


class DuckDBStore:
    def __init__(self, db_path: str | Path):
        self.db_path = resolve_path(db_path)
        self.conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.conn is None:
            self.conn = duckdb.connect(str(self.db_path))
        return self.conn

    def execute(self, sql: str, parameters: Iterable[object] | None = None):
        conn = self.connect()
        return conn.execute(sql) if parameters is None else conn.execute(sql, parameters)

    def upsert_dataframe(self, table_name: str, df: pd.DataFrame, primary_keys: list[str]) -> int:
        if df is None or df.empty:
            return 0
        conn = self.connect()
        temp_name = f"tmp_{table_name}"
        columns = list(df.columns)
        quoted_columns = ", ".join(f'"{col}"' for col in columns)
        join_clause = " AND ".join(f't."{key}" = s."{key}"' for key in primary_keys)
        conn.register(temp_name, df)
        try:
            conn.execute(f'DELETE FROM "{table_name}" AS t USING "{temp_name}" AS s WHERE {join_clause}')
            conn.execute(f'INSERT INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{temp_name}"')
        finally:
            conn.unregister(temp_name)
        return len(df)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
