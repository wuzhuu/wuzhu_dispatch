#!/usr/bin/env python3
"""补全 westock 数据 preclose 字段"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path

settings = load_settings()
store = LakeStore(db_path=resolve_db_path(settings))

# 找出所有 westock 且 preclose 为空的行
rows = store.execute("""
    SELECT ts_code, trade_date, close FROM daily_price
    WHERE source LIKE 'westock%'
      AND trade_date >= '2026-06-27'
      AND (preclose IS NULL OR preclose = 0)
    ORDER BY ts_code, trade_date
""").fetchall()
print(f"需要补 preclose: {len(rows)} 行", flush=True)

# 按 ts_code 分组
from collections import defaultdict
by_code = defaultdict(list)
for ts_code, trade_date, close in rows:
    by_code[ts_code].append((str(trade_date)[:10], close))

fixed = 0
for ts_code, dates in by_code.items():
    # 查该股票的历史 close
    prev = store.execute("""
        SELECT trade_date, close FROM daily_price
        WHERE ts_code = ? AND trade_date < ?
          AND close IS NOT NULL AND close > 0
        ORDER BY trade_date DESC LIMIT 1
    """, [ts_code, dates[0][0]]).fetchone()
    if prev is None:
        continue
    prev_date, prev_close = str(prev[0])[:10], prev[1]
    # 对每一行补 preclose
    for trade_date, close_val in dates:
        if pd.notna(prev_close) and prev_close > 0:
            store.execute("""
                UPDATE daily_price SET preclose = ?
                WHERE ts_code = ? AND trade_date = ? AND source LIKE 'westock%'
            """, [float(prev_close), ts_code, trade_date])
            fixed += 1
        # 当前 close 作为下一行的 preclose
        prev_close = close_val

print(f"已补 {fixed} 行 preclose", flush=True)

# 验证
remaining = store.execute("""
    SELECT COUNT(*) FROM daily_price
    WHERE source LIKE 'westock%'
      AND trade_date >= '2026-06-27'
      AND (preclose IS NULL OR preclose = 0)
""").fetchone()[0]
print(f"仍缺 preclose: {remaining} 行", flush=True)
store.close()
