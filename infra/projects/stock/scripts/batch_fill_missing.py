#!/usr/bin/env python3
"""
Batch fill missing 2026-06-01 daily price data (SSD-optimized local version).
Strategy:
1. Get stock list from local DB (no AKShare HTTP call needed)  
2. Find which ts_codes are missing today's data
3. OPEN ONE BaoStock session, fetch ALL missing remaining stocks
4. Do ONE upsert_daily_price call (avoids per-stock Parquet rewrite hell)
"""
import sys, time
from pathlib import Path
from datetime import datetime

import pandas as pd

PROJECT = Path.home() / "stock" / "stackAnalys"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "src"))

from src.utils.config import load_settings, resolve_db_path, resolve_lake_root
from src.storage.lake_store import LakeStore


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Batch fill missing daily price data")
    parser.add_argument("--target-date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--db-path", default=None,
                        help="DuckDB 路径（默认从 settings.yaml 读取）")
    args = parser.parse_args()

    target_date = args.target_date
    settings = load_settings()
    db_path = args.db_path or resolve_db_path(settings)

    # 1. Get stock list from LOCAL DB
    print(f"[1/4] Getting stock list from local DB ({db_path})...")
    store = LakeStore(db_path=db_path)
    try:
        rows = store.execute("SELECT DISTINCT ts_code FROM v_stock_basic_latest ORDER BY ts_code").fetchall()
        all_codes = [r[0] for r in rows]
    except Exception:
        rows = store.execute("SELECT DISTINCT ts_code FROM v_daily_price ORDER BY ts_code").fetchall()
        all_codes = [r[0] for r in rows]
    print(f"  Total stocks: {len(all_codes)}")

    # 2. Find missing, skip BJ stocks
    print("[2/4] Checking which stocks need data...")
    existing = set()
    try:
        rows = store.execute(
            f"SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = DATE '{target_date}'"
        ).fetchall()
        existing = {r[0] for r in rows}
    except Exception as e:
        print(f"  Query error: {e}")

    missing = [c for c in all_codes if c not in existing]
    bj_count = len([c for c in missing if c.startswith("bj.")])
    missing = [c for c in missing if not c.startswith("bj.")]
    print(f"  Already have: {len(existing)}, Need: {len(missing)+bj_count} (BJ: {bj_count}, skip)")
    print(f"  Fetchable (non-BJ): {len(missing)}")

    if not missing:
        print("[DONE] All non-BJ stocks already have data!")
        store.close()
        return
    print(f"  Range: {missing[0]} ~ {missing[-1]}")

    # 3. Batch-fetch via BaoStock — ONE login
    print("[3/4] Batch fetching via BaoStock...")
    import baostock as bs
    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    print("  BaoStock login OK")

    batch = []
    ok = 0
    failed = 0
    start = time.time()
    FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn,tradestatus,isST"

    try:
        for idx, ts_code in enumerate(missing):
            sys.stdout.write(f"\r  [{idx+1}/{len(missing)}] ok={ok} fail={failed} current={ts_code}        ")
            sys.stdout.flush()
            try:
                rs = bs.query_history_k_data_plus(
                    ts_code, FIELDS, start_date=target_date, end_date=target_date,
                    frequency="d", adjustflag="2",
                )
                if rs.error_code != "0":
                    failed += 1
                    continue
                rows_data = []
                while rs.next():
                    rows_data.append(rs.get_row_data())
                if not rows_data:
                    continue
                raw = pd.DataFrame(rows_data, columns=rs.fields)
                r = raw.iloc[0]
                batch.append({
                    "ts_code": ts_code,
                    "trade_date": pd.to_datetime(r["date"]).date(),
                    "open": pd.to_numeric(r["open"], errors="coerce"),
                    "high": pd.to_numeric(r["high"], errors="coerce"),
                    "low": pd.to_numeric(r["low"], errors="coerce"),
                    "close": pd.to_numeric(r["close"], errors="coerce"),
                    "preclose": pd.to_numeric(r["preclose"], errors="coerce"),
                    "volume": pd.to_numeric(r["volume"], errors="coerce"),
                    "amount": pd.to_numeric(r["amount"], errors="coerce"),
                    "pct_chg": pd.to_numeric(r["pctChg"], errors="coerce"),
                    "turn": pd.to_numeric(r["turn"], errors="coerce"),
                    "tradestatus": r["tradestatus"],
                    "is_st": r["isST"],
                    "adjust_type": "qfq",
                    "source": "baostock.query_history_k_data_plus",
                    "updated_at": pd.Timestamp.now(),
                })
                ok += 1
            except Exception:
                failed += 1
    finally:
        bs.logout()
        sys.stdout.write("\n")

    elapsed = time.time() - start
    print(f"  Fetch done: {ok} ok, {failed} fail, {elapsed:.0f}s ({ok/elapsed:.1f} stocks/s)")

    if not batch:
        print("[ABORT] No data fetched.")
        store.close()
        return

    # 4. ONE batch upsert
    print(f"[4/4] Writing {len(batch)} rows (single upsert)...")
    df = pd.DataFrame(batch)
    write_start = time.time()
    store.upsert_daily_price(df)
    write_elapsed = time.time() - write_start
    print(f"  Write done in {write_elapsed:.0f}s")

    new_count = store.execute(
        f"SELECT COUNT(*) FROM v_daily_price WHERE trade_date = DATE '{target_date}'"
    ).fetchone()[0]
    print(f"  Now have {new_count} stocks with {target_date} data")
    print("[DONE] Batch fill complete!")
    store.close()


if __name__ == "__main__":
    main()
