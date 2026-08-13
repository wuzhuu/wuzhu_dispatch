#!/usr/bin/env python3
"""快速修复 westock preclose — pandas groupby shift"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path

lake = Path("/mnt/data/stock/stock_local_ai_data/lake/daily_price")
files = sorted(lake.rglob("*.parquet"))
print(f"找到 {len(files)} 个 parquet 文件", flush=True)

all_westock = []
for pf in files:
    try:
        df = pd.read_parquet(pf)
    except:
        continue
    mask = df["source"].astype(str).str.contains("westock", na=False)
    if not mask.any():
        continue
    wk = df[mask].copy()
    wk["trade_date"] = pd.to_datetime(wk["trade_date"])
    all_westock.append(wk)
    print(f"  {pf.name}: {len(wk)} 行", flush=True)

if not all_westock:
    print("无 westock 数据需要修复", flush=True)
else:
    combined = pd.concat(all_westock, ignore_index=True)
    combined = combined.sort_values(["ts_code", "trade_date"])
    # 补 preclose
    combined["preclose"] = combined.groupby("ts_code")["close"].shift(1)
    has_prev = combined["preclose"].notna() & (combined["preclose"] > 0)
    print(f"补了 preclose: {has_prev.sum()} 行 (仍有 {len(combined) - has_prev.sum()} 行无前日数据)", flush=True)
    
    if has_prev.any():
        fix = combined[has_prev].copy()
        fix["trade_date"] = fix["trade_date"].dt.strftime("%Y-%m-%d")
        print(f"写入 {len(fix)} 行到 daily_price", flush=True)
        store = LakeStore(db_path=resolve_db_path(load_settings()))
        store.upsert_daily_price(fix)
        
        # 验证
        store.refresh_views()
        remain = store.execute("""
            SELECT COUNT(*) FROM daily_price
            WHERE source LIKE 'westock%' AND trade_date >= '2026-06-27'
              AND (preclose IS NULL OR preclose = 0)
        """).fetchone()[0]
        print(f"仍缺 preclose: {remain} 行", flush=True)
        store.close()

print("✅ 完成", flush=True)
