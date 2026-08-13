#!/usr/bin/env python3
"""
快速填充缺失的今日行情数据（2026-06-01）。
不修改业务代码，直接使用 AKShare 批量填充。
策略：
1. 检查 v_daily_price 看哪些 stocks 缺少今天数据
2. 对这些 stocks，直接用 AKShare stock_zh_a_hist 获取今日数据
3. 写入 DuckDB + Parquet 数据湖
"""
import os
import sys
import time
import random
import argparse
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd

# ── 项目路径 ──
PROJECT = Path.home() / "stock" / "stackAnalys"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "src"))

from src.utils.config import load_settings
from src.storage.lake_store import LakeStore


def parse_args():
    parser = argparse.ArgumentParser(description="快速填充今日缺失行情")
    parser.add_argument("--target-date", default="2026-06-01",
                        help="目标日期 YYYY-MM-DD")
    parser.add_argument("--db-path",
                        default="/mnt/sdcard/stock_local_ai_data/stock_data_v2.duckdb",
                        help="DuckDB 路径")
    parser.add_argument("--batch", type=int, default=50,
                        help="每次批量打印进度条数")
    return parser.parse_args()


def main():
    import sys
    sys.stdout.reconfigure(line_buffering=True)  # force line-buffered output
    args = parse_args()
    settings = load_settings()
    store = LakeStore(db_path=args.db_path)

    target = args.target_date
    print(f"[INFO] 目标日期: {target}")

    # 1. 获取股票列表（AKShare）
    import akshare as ak
    print("[INFO] 获取股票列表...")
    df_stock = ak.stock_info_a_code_name()
    all_codes = []
    for _, row in df_stock.iterrows():
        symbol = str(row.get("code") or row.get("代码", "")).zfill(6)
        if symbol.startswith("6"):
            ts_code = f"sh.{symbol}"
        elif symbol.startswith(("0", "3")):
            ts_code = f"sz.{symbol}"
        elif symbol.startswith(("4", "8", "9")):
            ts_code = f"bj.{symbol}"
        else:
            continue
        all_codes.append((symbol, ts_code))

    print(f"[INFO] 股票总数: {len(all_codes)}")

    # 2. 检查哪些已经存在今天数据
    existing = set()
    try:
        rows = store.execute(f"SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = DATE '{target}'").fetchall()
        existing = {r[0] for r in rows}
    except Exception:
        pass

    print(f"[INFO] 已有今日数据的 stocks: {len(existing)}")

    # 需要填充的
    missing = [(sym, tc) for sym, tc in all_codes if tc not in existing]
    print(f"[INFO] 需要填充的 stocks: {len(missing)}")

    if not missing:
        print("[DONE] 全部已完整")
        store.close()
        return

    # 3. 逐只调用 AKShare 获取今日数据
    import akshare as ak
    total = 0
    ok_count = 0
    fail_count = 0
    start = time.time()
    sleep_min = 0.3
    sleep_max = 0.8

    for idx, (symbol, ts_code) in enumerate(missing):
        try:
            time.sleep(random.uniform(sleep_min, sleep_max))
            raw = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=target.replace("-", ""),
                end_date=target.replace("-", ""),
                adjust="qfq",
            )
            if raw.empty:
                # 可能停牌或非交易日
                continue

            # 标准化
            df = pd.DataFrame()
            df["ts_code"] = ts_code
            df["trade_date"] = pd.to_datetime(raw.get("日期", target), errors="coerce").dt.date
            df["open"] = pd.to_numeric(raw.get("开盘", 0), errors="coerce")
            df["high"] = pd.to_numeric(raw.get("最高", 0), errors="coerce")
            df["low"] = pd.to_numeric(raw.get("最低", 0), errors="coerce")
            df["close"] = pd.to_numeric(raw.get("收盘", 0), errors="coerce")
            df["preclose"] = pd.to_numeric(raw.get("昨收", 0), errors="coerce") if "昨收" in raw.columns else 0.0
            df["volume"] = pd.to_numeric(raw.get("成交量", 0), errors="coerce")
            df["amount"] = pd.to_numeric(raw.get("成交额", 0), errors="coerce")
            df["pct_chg"] = pd.to_numeric(raw.get("涨跌幅", 0), errors="coerce")
            df["turn"] = pd.to_numeric(raw.get("换手率", 0), errors="coerce")
            df["tradestatus"] = 1
            df["is_st"] = raw.get("是否ST", 0) if "是否ST" in raw.columns else 0
            df["adjust_type"] = "qfq"
            df["source"] = "akshare.stock_zh_a_hist.qfq"
            df["updated_at"] = pd.Timestamp.now()

            store.upsert_daily_price(df)
            total += len(df)
            ok_count += 1

        except Exception as exc:
            fail_count += 1
            if fail_count <= 5:
                print(f"  FAIL {ts_code}: {exc}")

        if (idx + 1) % args.batch == 0:
            elapsed = time.time() - start
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{idx+1}/{len(missing)}] ok={ok_count} fail={fail_count} total_rows={total} {rate:.1f} stocks/s")

    elapsed = time.time() - start
    print(f"\n[DONE] 填充完成!")
    print(f"  处理 {len(missing)} 只股票")
    print(f"  成功: {ok_count}, 失败: {fail_count}")
    print(f"  写入行数: {total}")
    print(f"  耗时: {elapsed:.1f}s ({elapsed/60:.1f}m)")
    print(f"  速度: {len(missing)/elapsed:.1f} stocks/s")

    store.close()


if __name__ == "__main__":
    main()
