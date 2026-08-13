from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.storage.lake_store import LakeStore


def _row(table_name: str, trade_date: str, check_name: str, status: str, message: str) -> dict[str, str]:
    return {
        "check_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "table_name": table_name,
        "trade_date": trade_date,
        "check_name": check_name,
        "status": status,
        "message": message,
    }


def run_quality_checks(store: LakeStore, trade_date: str, sample_mode: bool = False) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    stock_count = store.execute("SELECT COUNT(*) FROM v_stock_basic_latest").fetchone()[0]
    rows.append(_row("stock_basic", trade_date, "stock_count_gt_4000", "PASS" if stock_count > 4000 else "FAIL", f"count={stock_count}"))

    daily_count = store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [trade_date]).fetchone()[0]
    if sample_mode:
        rows.append(_row("daily_price", trade_date, "target_date_rows_gt_3000", "SKIP", f"sample_mode rows={daily_count}"))
    else:
        rows.append(_row("daily_price", trade_date, "target_date_rows_gt_3000", "PASS" if daily_count > 3000 else "FAIL", f"rows={daily_count}"))

    index_count = store.execute("SELECT COUNT(*) FROM v_index_daily WHERE trade_date = ?", [trade_date]).fetchone()[0]
    rows.append(_row("index_daily", trade_date, "target_date_rows_ge_3", "PASS" if index_count >= 3 else "FAIL", f"rows={index_count}"))

    dup_count = store.execute("""
        SELECT COUNT(*) FROM (
            SELECT ts_code, trade_date, COUNT(*) c
            FROM v_daily_price
            GROUP BY ts_code, trade_date
            HAVING c > 1
        )
    """).fetchone()[0]
    rows.append(_row("daily_price", trade_date, "no_duplicate_pk", "PASS" if dup_count == 0 else "FAIL", f"duplicates={dup_count}"))

    null_ohlc = store.execute("""
        SELECT COUNT(*) FROM v_daily_price
        WHERE trade_date = ? AND (open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL)
    """, [trade_date]).fetchone()[0]
    rows.append(_row("daily_price", trade_date, "ohlc_not_null", "PASS" if null_ohlc == 0 else "FAIL", f"bad_rows={null_ohlc}"))

    high_low_bad = store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ? AND high < low", [trade_date]).fetchone()[0]
    rows.append(_row("daily_price", trade_date, "high_ge_low", "PASS" if high_low_bad == 0 else "FAIL", f"bad_rows={high_low_bad}"))

    close_bad = store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ? AND close <= 0", [trade_date]).fetchone()[0]
    rows.append(_row("daily_price", trade_date, "close_gt_0", "PASS" if close_bad == 0 else "FAIL", f"bad_rows={close_bad}"))

    volume_bad = store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ? AND volume < 0", [trade_date]).fetchone()[0]
    rows.append(_row("daily_price", trade_date, "volume_ge_0", "PASS" if volume_bad == 0 else "FAIL", f"bad_rows={volume_bad}"))

    df = pd.DataFrame(rows)
    store.upsert_dataframe("data_quality_log", df, ["check_time", "table_name", "trade_date", "check_name"])
    return df
