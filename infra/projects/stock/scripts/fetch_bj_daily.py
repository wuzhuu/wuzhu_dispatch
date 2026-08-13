#!/usr/bin/env python3
"""Fetch 北京证券交易所 (BSE) daily price data from Tencent API, write to LakeStore.
Rate: exactly 1 request per stock, with 1.0-1.5s sleep between stocks (≤1 req/s).
"""

import json
import os
import time
import sys
from datetime import datetime, date
from pathlib import Path

# ── Re-exec with project venv ──
PROJECT_DIR = Path(__file__).resolve().parents[1] / "stackAnalys"
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python3"
if sys.executable != str(VENV_PYTHON):
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)

import pandas as pd
import requests

sys.path.insert(0, str(PROJECT_DIR))
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path, resolve_lake_root

# ── Constants ──
TENCENT_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
REQUEST_SLEEP = 1.2  # seconds between stocks, ensures <1 req/s

# Raw Tencent row format: [date, open, close, high, low, volume, {}, pctChg, amount, ...]
# volume unit: 100 shares (手), amount unit: 万元


def get_bj_stocks() -> list[dict]:
    """Fetch BSE stock list from official BSE website via akshare."""
    import akshare as ak
    df = ak.stock_info_bj_name_code()
    stocks = []
    for _, row in df.iterrows():
        code = str(row["证券代码"]).zfill(6)
        stocks.append({
            "code": code,
            # Tencent API uses 'bj920000' (no dot)
            "tencent_symbol": f"bj{code}",
            # Project convention uses 'bj.920000' (with dot)
            "ts_code": f"bj.{code}",
            "name": row.get("证券简称", ""),
        })
    return stocks


def fetch_tencent(symbol: str) -> list[list]:
    """Single API call to Tencent, returns all daily kline rows.
    Uses no-adjust mode to get raw data (key='day').
    Makes exactly 1 HTTP request.
    """
    params = {
        "_var": "kline_day_all",
        "param": f"{symbol},day,2023-01-01,{datetime.now().year + 1}-12-31,640,",
        "r": f"{time.time():.6f}",
    }
    try:
        r = requests.get(TENCENT_URL, params=params, timeout=60)
        r.raise_for_status()
        raw = r.text
        json_str = raw[raw.find("={") + 1:]
        data = json.loads(json_str)
        stock_data = data.get("data", {}).get(symbol, {})
        rows = stock_data.get("day") or stock_data.get("qfqday") or []
        return rows
    except Exception as e:
        print(f"  [ERR] Tencent API failed for {symbol}: {e}", flush=True)
        return []


def to_dataframe(raw_rows: list[list], ts_code: str) -> pd.DataFrame:
    """Convert Tencent raw rows → daily_price DataFrame with proper ts_code format."""
    if not raw_rows:
        return pd.DataFrame()
    records = []
    for row in raw_rows:
        if not row or len(row) < 9:
            continue
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
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    df["ts_code"] = ts_code  # e.g. "bj.920000"
    df["preclose"] = None
    df["turn"] = None
    df["tradestatus"] = None
    df["is_st"] = None
    df["adjust_type"] = ""
    df["source"] = "tencent.kline_day"
    df["source_priority"] = 60
    df["updated_at"] = pd.Timestamp.now()
    return df.sort_values("trade_date").reset_index(drop=True)


def run(db_path: str | Path, lake_root: str | Path, max_stocks: int = 0, sleep: float = 1.2):
    store = LakeStore(db_path=db_path, lake_root=lake_root)

    # 1. Get stock list
    print("Fetching BSE stock list...", flush=True)
    all_stocks = get_bj_stocks()
    print(f"  Total BSE stocks: {len(all_stocks)}", flush=True)

    # 2. Check which already have adequate data (at least recent 30 days in db)
    conn = store.connect()
    existing = conn.execute(
        "SELECT ts_code, MAX(trade_date), COUNT(*) FROM v_daily_price WHERE ts_code LIKE 'bj.%' GROUP BY ts_code"
    ).fetchall()
    existing_map = {r[0]: {"max_date": r[1], "count": r[2]} for r in existing}
    print(f"  Already in DB: {len(existing_map)} stocks", flush=True)

    # Filter stocks to process
    to_process = []
    for s in all_stocks:
        ex = existing_map.get(s["ts_code"])
        if ex:
            # Already has data, only process if it's very incomplete (<30 rows) or stale (>7 days old)
            need_update = False
            if ex["count"] < 30:
                need_update = True
            elif ex["max_date"]:
                days_stale = (date.today() - ex["max_date"]).days
                if days_stale > 7:
                    need_update = True
            if need_update:
                to_process.append(s)
        else:
            to_process.append(s)

    print(f"  Need to fetch: {len(to_process)} stocks", flush=True)

    if max_stocks > 0:
        to_process = to_process[:max_stocks]

    print(f"  Will process: {len(to_process)} stocks", flush=True)
    print()

    # 3. Fetch each stock at slow speed
    total_written = 0
    ok_count = 0
    fail_count = 0

    for i, stock in enumerate(to_process):
        ts_code = stock["ts_code"]
        tenc = stock["tencent_symbol"]
        name = stock["name"]

        print(f"[{i+1}/{len(to_process)}] {ts_code} ({name})...", end=" ", flush=True)

        # One API call per stock
        rows = fetch_tencent(tenc)
        if not rows:
            print("NO DATA", flush=True)
            fail_count += 1
            time.sleep(sleep)
            continue

        df = to_dataframe(rows, ts_code)
        if df.empty:
            print("EMPTY", flush=True)
            fail_count += 1
            time.sleep(sleep)
            continue

        # Write to lake store
        try:
            written = store.upsert_daily_price(df)
            total_written += written
            ok_count += 1
            print(f"{len(df)} rows → {written} written", flush=True)
        except Exception as e:
            print(f"WRITE FAIL: {e}", flush=True)
            fail_count += 1

        # Sleep between stocks: exactly 1 req/s max
        if i < len(to_process) - 1:
            time.sleep(sleep)

    print()
    print(f"=== Done: {ok_count} OK, {fail_count} FAIL, {total_written} total rows written ===")
    store.close()


def main():
    settings = load_settings()
    db_path = resolve_db_path(settings)
    lake_root = resolve_lake_root(settings)

    print("=" * 60)
    print(" 北交所 Daily Price Fetcher (Tencent API, slow mode)")
    print(f" Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f" Target: {db_path}")
    print(f" Lake:   {lake_root}")
    print(" Rate: ≤1 req/s")
    print("=" * 60)
    print()

    # Parse max_stocks from CLI
    max_stocks = 0
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        max_stocks = int(sys.argv[1])

    run(db_path=db_path, lake_root=lake_root, max_stocks=max_stocks, sleep=REQUEST_SLEEP)

    print(f" Finished: {datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
