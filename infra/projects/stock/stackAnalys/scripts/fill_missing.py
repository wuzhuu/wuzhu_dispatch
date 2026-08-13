#!/usr/bin/env python3
"""补采缺失日线 — baostock + eastmoney 双线程"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.collectors.market_provider import MarketDataProvider
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path


def fill(target_date: str, store):
    have = set(r[0] for r in store.execute(
        "SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = ?", [target_date]
    ).fetchall())
    all_stocks = [r[0] for r in store.execute(
        "SELECT DISTINCT ts_code FROM stock_basic"
    ).fetchall()]
    missing = [s for s in all_stocks if s not in have]
    total = len(missing)
    print(f"\n{target_date}: 缺失 {total}/{len(all_stocks)} 只", flush=True)
    if not total:
        print("  ✅ 无缺失", flush=True)
        return

    # 每线程独立 provider
    def worker(codes):
        p = MarketDataProvider()
        frames = []
        for c in codes:
            try:
                df = p.get_daily_price_multi_source(c, target_date, target_date,
                    ["baostock", "eastmoney"])
                if df is not None and not df.empty:
                    frames.append(df)
            except:
                pass
        p.close_baostock_session()
        return frames

    # 分 4 组
    n = max(1, total // 4)
    groups = [missing[i:i+n] for i in range(0, total, n)]
    print(f"  分 {len(groups)} 组并行...", flush=True)

    batch = []
    written = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        fs = {ex.submit(worker, g): i for i, g in enumerate(groups)}
        for fut in as_completed(fs):
            idx = fs[fut]
            try:
                frames = fut.result()
                for df in frames:
                    batch.append(df)
                    written += len(df)
                    if len(batch) >= 200:
                        store.upsert_daily_price(pd.concat(batch, ignore_index=True))
                        batch = []
                print(f"  组{idx} 完成: {len(frames)} 只", flush=True)
            except Exception as e:
                print(f"  组{idx} 异常: {e}", flush=True)

    if batch:
        store.upsert_daily_price(pd.concat(batch, ignore_index=True))

    final = store.execute(
        "SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [target_date]
    ).fetchone()[0]
    print(f"  ✅ {target_date}: {final} 行 (+{final - (len(all_stocks) - total)})", flush=True)


def main():
    store = LakeStore(db_path=resolve_db_path(load_settings()))
    fill("2026-06-30", store)
    fill("2026-06-19", store)
    store.close()


if __name__ == "__main__":
    main()
