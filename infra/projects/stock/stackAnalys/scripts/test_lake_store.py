from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.analysis.data_loader import StockDataLoader


def _daily(rows: list[dict]) -> pd.DataFrame:
    base = {
        "ts_code": "sz.000001",
        "trade_date": "2024-01-02",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "preclose": 10.0,
        "volume": 1000.0,
        "amount": 10000.0,
        "pct_chg": 5.0,
        "turn": 1.0,
        "tradestatus": "1",
        "is_st": "0",
        "adjust_type": "qfq",
        "source": "akshare.stock_zh_a_hist.qfq",
        "updated_at": "2024-01-02 10:00:00",
    }
    return pd.DataFrame([{**base, **row} for row in rows])


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="stock_lake_test_"))
    try:
        store = LakeStore(db_path=tmp / "stock.duckdb", lake_root=tmp / "lake")
        store.upsert_daily_price(
            _daily(
                [
                    {"source": "akshare.stock_zh_a_hist.qfq", "close": 10.5, "updated_at": "2024-01-02 10:00:00"},
                    {"source": "yahoo.finance", "close": 9.5, "updated_at": "2024-01-02 12:00:00"},
                ]
            )
        )
        first_count = store.execute("SELECT COUNT(*) FROM v_daily_price").fetchone()[0]
        first_close = store.execute("SELECT close FROM v_daily_price WHERE ts_code = 'sz.000001'").fetchone()[0]
        assert first_count == 1, f"dedupe failed, count={first_count}"
        assert first_close == 10.5, f"source priority failed, close={first_close}"

        store.upsert_daily_price(
            _daily(
                [
                    {"source": "baostock.query_history_k_data_plus", "close": 12.5, "updated_at": "2024-01-02 09:00:00"},
                    {"trade_date": "2024-02-01", "close": 13.0, "source": "akshare.stock_zh_a_hist.qfq"},
                ]
            )
        )
        rows = store.execute("SELECT ts_code, trade_date, close, source_priority FROM v_daily_price ORDER BY trade_date").fetchall()
        assert len(rows) == 2, rows
        assert rows[0][2] == 12.5 and rows[0][3] == 100, rows
        assert any((store.lake_root / "backups").rglob("*.parquet")), "backup parquet not created"

        duplicate_partition = store.lake_root / "daily_price" / "adjust_type=qfq" / "year=2024" / "month=01"
        duplicate_file = duplicate_partition / "manual-duplicate.parquet"
        _daily([{"source": "yahoo.finance", "close": 1.0, "updated_at": "2024-01-02 13:00:00"}]).to_parquet(duplicate_file, index=False)
        store.refresh_views()
        duplicate_count = store.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT ts_code, trade_date, adjust_type, COUNT(*) AS c
                FROM v_daily_price
                GROUP BY 1, 2, 3
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert duplicate_count == 1, duplicate_count
        dedupe_stats = store.dedupe_daily_price_partitions()
        assert dedupe_stats["partitions_rewritten"] == 1, dedupe_stats
        duplicate_count = store.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT ts_code, trade_date, adjust_type, COUNT(*) AS c
                FROM v_daily_price
                GROUP BY 1, 2, 3
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert duplicate_count == 0, duplicate_count

        index_df = pd.DataFrame(
            [
                {
                    "index_code": "sh.000300",
                    "trade_date": "2024-01-02",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 1.0,
                    "close": 1.5,
                    "preclose": 1.4,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "pct_chg": 1.0,
                    "source": "stooq",
                    "updated_at": "2024-01-02 10:00:00",
                }
            ]
        )
        store.upsert_index_daily(index_df)
        assert store.execute("SELECT COUNT(*) FROM v_index_daily").fetchone()[0] == 1

        basic_df = pd.DataFrame(
            [
                {
                    "ts_code": "sz.000001",
                    "symbol": "000001",
                    "name": "Ping An Bank",
                    "area": None,
                    "industry": None,
                    "market": None,
                    "list_date": pd.NaT,
                    "exchange": "sz",
                    "is_hs": None,
                    "updated_at": pd.Timestamp("2024-01-02 10:00:00"),
                }
            ]
        )
        store.write_stock_basic_snapshot(basic_df, snapshot_date="2024-01-02")
        assert store.execute("SELECT name FROM v_stock_basic_latest WHERE ts_code = 'sz.000001'").fetchone()[0] == "Ping An Bank"
        industry_map_df = pd.DataFrame(
            [
                {
                    "ts_code": "sz.000001",
                    "industry_name": "银行",
                    "sw_code_2021": "801780.SI",
                    "source": "legacy_stock_basic",
                    "fetched_at": pd.Timestamp("2024-01-02 10:00:00"),
                }
            ]
        )
        store.upsert_stock_industry_map(industry_map_df)
        assert store.execute("SELECT industry_name FROM stock_industry_map WHERE ts_code = 'sz.000001'").fetchone()[0] == "银行"

        industry_df = pd.DataFrame(
            [
                {
                    "板块代码": "BK0475",
                    "板块名称": "银行",
                    "最新价": 1000.0,
                    "涨跌额": 10.0,
                    "涨跌幅": 1.0,
                    "总市值": 100000000.0,
                    "换手率": 0.5,
                    "上涨家数": 10,
                    "下跌家数": 2,
                    "领涨股票": "Ping An Bank",
                    "领涨股票-涨跌幅": 2.5,
                    "board_type": "industry",
                    "source": "akshare.stock_board_industry_name_em",
                    "updated_at": pd.Timestamp("2024-01-02 10:00:00"),
                }
            ]
        )
        store.write_industry_board_snapshot(industry_df, snapshot_date="2024-01-02")
        board_row = store.execute("SELECT board_code, board_name FROM v_industry_board_latest WHERE board_name = '银行'").fetchone()
        assert board_row == ("BK0475", "银行"), board_row

        manifest_count = store.execute("SELECT COUNT(*) FROM dataset_manifest").fetchone()[0]
        unique_count = store.execute("SELECT COUNT(*) FROM unique_key_index").fetchone()[0]
        coverage_count = store.execute("SELECT COUNT(*) FROM symbol_coverage").fetchone()[0]
        assert manifest_count >= 5, manifest_count
        assert unique_count >= 3, unique_count
        assert coverage_count >= 2, coverage_count
        assert store.execute("SELECT COUNT(*) FROM dataset_manifest WHERE dataset_name = 'industry_board'").fetchone()[0] == 1
        db_path = store.db_path
        store.close()
        loader = StockDataLoader(db_path)
        try:
            basic = loader.get_stock_basic()
            assert basic.loc[basic["ts_code"] == "sz.000001", "industry"].iloc[0] == "银行"
            assert "stock_basic_industry" in basic.columns
        finally:
            loader.close()
        print("LakeStore tests passed: upsert, dedupe, backup, manifest, coverage, unique index, snapshots, views.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
