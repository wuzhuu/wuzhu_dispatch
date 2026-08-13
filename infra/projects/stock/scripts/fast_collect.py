#!/usr/bin/env python3
"""Fast parallel data collection for 2026-07-17 using baostock.

Bypasses the regular pipeline slowness by using direct baostock queries
with multiple parallel workers.
"""
from __future__ import annotations

import concurrent.futures
import sys
import time
from pathlib import Path

import baostock as bs
import duckdb
import pandas as pd

TODAY = "2026-07-17"
DB_PATH = "/mnt/data/stock/stock_local_ai_data/stock_data_v2.duckdb"
MAX_WORKERS = 16
BATCH_SIZE = 500  # Write to DB every N stocks


def get_all_stock_codes() -> list[str]:
    """Get all A-share stock codes from baostock."""
    lg = bs.login()
    assert lg.error_code == "0", f"baostock login failed: {lg.error_msg}"

    rs = bs.query_all_stock(day=TODAY)
    codes: list[str] = []
    while rs.next():
        row = rs.get_row_data()
        code = row[0]  # e.g. "sh.600000"
        if code.startswith("sh.") or code.startswith("sz.") or code.startswith("bj."):
            codes.append(code)
    bs.logout()
    print(f"Total stocks from baostock: {len(codes)}")
    return codes


def fetch_one_stock(code: str) -> pd.DataFrame | None:
    """Fetch today's daily price for one stock."""
    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,preclose,volume,amount,pctChg",
            start_date=TODAY,
            end_date=TODAY,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        # Convert numeric columns
        for col in ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["trade_date"] = TODAY
        df["source"] = "baostock.query_history_k_data_plus"
        df["updated_at"] = pd.Timestamp.now()
        return df
    except Exception as e:
        print(f"  Error fetching {code}: {e}", file=sys.stderr)
        return None


def write_batch(conn: duckdb.DuckDBPyConnection, batch: list[pd.DataFrame], table: str = "daily_price") -> int:
    """Write a batch of dataframes to DuckDB."""
    if not batch:
        return 0
    combined = pd.concat(batch, ignore_index=True)
    if combined.empty:
        return 0

    # Use the lake_store upsert pattern
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.register("_temp_df", combined)
        cols = ", ".join(f'"{c}"' for c in combined.columns)
        conn.execute(
            f'INSERT OR REPLACE INTO "{table}" ({cols}) SELECT {cols} FROM _temp_df'
        )
        conn.execute("COMMIT")
        return len(combined)
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"  Write error: {e}", file=sys.stderr)
        return 0


def fetch_index_daily(conn: duckdb.DuckDBPyConnection) -> int:
    """Fetch today's index data from baostock."""
    index_codes = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]
    bs.login()
    total = 0
    for code in index_codes:
        try:
            rs = bs.query_history_k_data_plus(
                code,
                "date,code,open,high,low,close,preclose,volume,amount,pctChg",
                start_date=TODAY,
                end_date=TODAY,
                frequency="d",
                adjustflag="3",
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["trade_date"] = TODAY
                df["source"] = "baostock.query_history_k_data_plus.index"
                df["updated_at"] = pd.Timestamp.now()
                conn.register("_idx_df", df)
                cols = ", ".join(f'"{c}"' for c in df.columns)
                conn.execute(f'INSERT OR REPLACE INTO index_daily ({cols}) SELECT {cols} FROM _idx_df')
                total += len(df)
                print(f"  Index {code}: {len(df)} rows")
        except Exception as e:
            print(f"  Index {code} error: {e}", file=sys.stderr)
    bs.logout()
    return total


def main():
    t0 = time.time()
    print(f"=== Fast data collection for {TODAY} ===")

    # Connect to DuckDB
    conn = duckdb.connect(DB_PATH)
    print(f"DB connected: {DB_PATH}")

    # Get all stock codes
    codes = get_all_stock_codes()
    if not codes:
        print("No stocks to process!")
        return

    # Fetch daily prices in parallel
    total_rows = 0
    batch: list[pd.DataFrame] = []
    processed = 0
    failed = 0

    # Login once for all workers
    lg = bs.login()
    assert lg.error_code == "0", f"Login failed: {lg.error_msg}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        fut_map = {executor.submit(fetch_one_stock, code): code for code in codes}
        for future in concurrent.futures.as_completed(fut_map):
            code = fut_map[future]
            try:
                df = future.result()
                processed += 1
                if df is not None and not df.empty:
                    batch.append(df)
                    total_rows += len(df)
                elif df is None:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"  Failed {code}: {e}", file=sys.stderr)

            # Write batch periodically
            if len(batch) >= BATCH_SIZE:
                written = write_batch(conn, batch)
                batch = []

            if processed % 500 == 0:
                elapsed = time.time() - t0
                print(f"  Progress: {processed}/{len(codes)} stocks, "
                      f"{total_rows} rows, {failed} failed, "
                      f"{elapsed:.0f}s elapsed", flush=True)

    # Final batch write
    if batch:
        written = write_batch(conn, batch)
        batch = []

    bs.logout()

    elapsed = time.time() - t0
    print(f"\nDaily price collection done: {total_rows} rows, {failed} failed, {elapsed:.0f}s")

    # Fetch index daily
    print("\nFetching index daily...")
    index_rows = fetch_index_daily(conn)
    print(f"Index daily: {index_rows} rows")

    # Verify
    r = conn.execute(f"SELECT COUNT(*) FROM daily_price WHERE trade_date = '{TODAY}'").fetchone()
    print(f"\nFinal verification: daily_price {TODAY} = {r[0]} rows")
    r = conn.execute(f"SELECT COUNT(*) FROM index_daily WHERE trade_date = '{TODAY}'").fetchone()
    print(f"Final verification: index_daily {TODAY} = {r[0]} rows")

    conn.close()
    print(f"\nTotal time: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
