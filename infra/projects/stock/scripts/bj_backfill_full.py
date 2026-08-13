#!/usr/bin/env python3
"""Backfill 北交所 full history from Tencent API (3 requests per stock).
The single-request approach only got the latest 640 rows, missing pre-2023 data.
This splits into 3 year chunks: 2021-2022, 2023-2024, 2025-2026.
Rate: 1s between requests = ~3s per stock.
"""

import json
import os
import time
import sys
from datetime import datetime, date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1] / "stackAnalys"
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python3"
if sys.executable != str(VENV_PYTHON):
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)

import pandas as pd
import requests

sys.path.insert(0, str(PROJECT_DIR))
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path, resolve_lake_root

TENCENT_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SLEEP = 1.0  # 1s between requests

# Year chunks that each fit within the 640-row API limit
YEAR_CHUNKS = [(2021, 2023), (2023, 2025), (2025, 2027)]


def get_bj_stocks() -> list[dict]:
    import akshare as ak
    df = ak.stock_info_bj_name_code()
    stocks = []
    for _, row in df.iterrows():
        code = str(row["证券代码"]).zfill(6)
        stocks.append({
            "code": code,
            "tencent_symbol": f"bj{code}",
            "ts_code": f"bj.{code}",
            "name": row.get("证券简称", ""),
        })
    return stocks


def fetch_chunk(symbol: str, start_year: int, end_year: int) -> list[list]:
    """Single Tencent API call for a year range. Returns raw rows."""
    params = {
        "_var": f"kline_day{start_year}",
        "param": f"{symbol},day,{start_year}-01-01,{end_year}-01-01,640,",
        "r": f"{time.time():.6f}",
    }
    try:
        r = requests.get(TENCENT_URL, params=params, timeout=30)
        r.raise_for_status()
        js = json.loads(r.text[r.text.find("={") + 1:])
        stock = js.get("data", {}).get(symbol, {})
        return stock.get("day") or stock.get("qfqday") or []
    except Exception as e:
        print(f"    [ERR] chunk {start_year}-{end_year-1}: {e}", flush=True)
        return []


def to_dataframe(all_raw_rows: list[list], ts_code: str) -> pd.DataFrame:
    """Dedup across chunks, convert to daily_price format."""
    if not all_raw_rows:
        return pd.DataFrame()
    # Dedup by date
    seen = set()
    unique = []
    for row in all_raw_rows:
        if row and len(row) >= 9 and row[0] not in seen:
            seen.add(row[0])
            unique.append(row)
    if not unique:
        return pd.DataFrame()

    records = []
    for row in unique:
        try:
            records.append({
                "trade_date": str(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]) * 100,
                "amount": float(row[8]) * 10000,
                "pct_chg": float(row[7]) if row[7] else 0.0,
            })
        except (ValueError, TypeError, IndexError):
            continue
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    df["ts_code"] = ts_code
    df["preclose"] = None
    df["turn"] = None
    df["tradestatus"] = None
    df["is_st"] = None
    df["adjust_type"] = ""
    df["source"] = "tencent.kline_day.full"
    df["source_priority"] = 60
    df["updated_at"] = pd.Timestamp.now()
    return df


def run(db_path, lake_root, max_stocks=0):
    store = LakeStore(db_path=db_path, lake_root=lake_root)

    print("Fetching BSE stock list...", flush=True)
    all_stocks = get_bj_stocks()
    print(f"  Total: {len(all_stocks)}", flush=True)

    # Check current coverage
    existing_info = {}
    for s in all_stocks:
        r = store.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date), ANY_VALUE(source) FROM v_daily_price WHERE ts_code = ? GROUP BY ts_code",
            [s["ts_code"]],
        ).fetchone()
        if r:
            existing_info[s["ts_code"]] = {
                "rows": r[0], "first": r[1], "last": r[2], "source": str(r[3]),
            }

    # Decide which stocks need backfill:
    # - If first date is late 2023 or later → missing early history
    # - If row count is <600 and stock listed before 2024 → incomplete
    to_process = []
    for s in all_stocks:
        ex = existing_info.get(s["ts_code"])
        if ex:
            # Check if data starts after early 2023 → likely truncated
            first_year = int(str(ex["first"])[:4]) if ex["first"] else 0
            if first_year >= 2023 and ex["rows"] >= 640:
                to_process.append(s)
            elif ex["rows"] < 30:
                to_process.append(s)
        else:
            to_process.append(s)

    print(f"  Need backfill: {len(to_process)} stocks", flush=True)
    if max_stocks > 0:
        to_process = to_process[:max_stocks]
    print(f"  Will process: {len(to_process)} stocks", flush=True)
    print(f"  Chunks per stock: {len(YEAR_CHUNKS)} ({', '.join(f'{a}-{b-1}' for a,b in YEAR_CHUNKS)})", flush=True)
    print(f"  Est. time: {len(to_process) * len(YEAR_CHUNKS) * SLEEP / 60:.1f} min", flush=True)
    print()

    total_written = 0
    ok_count = 0
    fail_count = 0

    for i, stock in enumerate(to_process):
        ts_code = stock["ts_code"]
        tenc = stock["tencent_symbol"]
        name = stock["name"]
        ex = existing_info.get(ts_code, {})

        print(f"[{i+1}/{len(to_process)}] {ts_code} ({name})", flush=True)
        if ex:
            print(f"  current: {ex['rows']} rows, {ex['first']} ~ {ex['last']} (source={ex['source']})", flush=True)

        # 3 API calls for 3 year chunks
        all_raw = []
        for start, end in YEAR_CHUNKS:
            rows = fetch_chunk(tenc, start, end)
            all_raw.extend(rows)
            time.sleep(SLEEP)

        if not all_raw:
            print(f"  NO DATA", flush=True)
            fail_count += 1
            continue

        df = to_dataframe(all_raw, ts_code)
        if df.empty:
            print(f"  EMPTY after conversion", flush=True)
            fail_count += 1
            continue

        try:
            written = store.upsert_daily_price(df)
            total_written += written
            ok_count += 1
            first_date = df["trade_date"].iloc[0]
            last_date = df["trade_date"].iloc[-1]
            # Convert YYYYMMDD to YYYY-MM-DD for display
            first_d = f"{first_date[:4]}-{first_date[4:6]}-{first_date[6:]}"
            last_d = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
            print(f"  ✓ {len(df)} rows ({first_d} ~ {last_d})", flush=True)
        except Exception as e:
            print(f"  WRITE FAIL: {e}", flush=True)
            fail_count += 1

    print()
    print(f"=== Done: {ok_count} OK, {fail_count} FAIL, {total_written} total rows ===")
    store.close()


def main():
    settings = load_settings()
    db_path = resolve_db_path(settings)
    lake_root = resolve_lake_root(settings)

    print("=" * 60)
    print(" 北交所 Full History Backfill (Tencent 3-chunk)")
    print(f" Target: {db_path}")
    print(f" Rate:   1 req/s = ~3s/stock")
    print("=" * 60)
    print()

    max_stocks = 0
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        max_stocks = int(sys.argv[1])

    run(db_path=db_path, lake_root=lake_root, max_stocks=max_stocks)


if __name__ == "__main__":
    main()
