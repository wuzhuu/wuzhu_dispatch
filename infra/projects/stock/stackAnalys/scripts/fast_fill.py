#!/usr/bin/env python3
"""快速补采剩余6/30缺失 — westock串行批量"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import subprocess, pandas as pd
from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path

def parse_table(stdout):
    lines = [l for l in stdout.strip().split("\n") if l.startswith("|")]
    if len(lines) < 3: return []
    hdr = [p.strip().lower() for p in lines[0].split("|")[1:-1]]
    has_sym = "symbol" in hdr
    rows = []
    for line in lines[2:]:
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if has_sym and len(parts) >= 8:
            rows.append({"sym": parts[0], "d": parts[1], "o": parts[2], "c": parts[3],
                         "h": parts[4], "l": parts[5], "v": parts[6], "a": parts[7]})
        elif not has_sym and len(parts) >= 7:
            rows.append({"sym": None, "d": parts[0], "o": parts[1], "c": parts[2],
                         "h": parts[3], "l": parts[4], "v": parts[5], "a": parts[6]})
    return rows

store = LakeStore(db_path=resolve_db_path(load_settings()))
target = "2026-06-30"
have = set(r[0] for r in store.execute(
    "SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = ?", [target]
).fetchall())
# 只补正常日出现过数据的股票（避免补指数/非A股）
ref_date = "2026-06-29"
normal = set(r[0] for r in store.execute(
    "SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = ?", [ref_date]
).fetchall())
missing = [s for s in normal if s not in have]
print(f"剩余 {len(missing)} 只 (参考日{ref_date}有{len(normal)}只)", flush=True)

BATCH = 80  # 每批80个代码
codes = [s.replace(".","") for s in missing]
batches = [codes[i:i+BATCH] for i in range(0, len(codes), BATCH)]
written = 0
for bi, batch in enumerate(batches):
    try:
        r = subprocess.run(["npx", "-y", "westock-data-skillhub@1.0.3",
            "kline", ",".join(batch), "--period", "day", "--limit", "5"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0: continue
        recs = parse_table(r.stdout)
        target_rows = [rec for rec in recs if rec["d"] == target]
        if not target_rows: continue
        rows_data = []
        for rec in target_rows:
            sym = rec["sym"] or batch[0]
            tc = sym[:2] + "." + sym[2:] if "." not in sym else sym
            rows_data.append({"ts_code": tc, "trade_date": rec["d"],
                "open": float(rec["o"]), "high": float(rec["h"]),
                "low": float(rec["l"]), "close": float(rec["c"]),
                "volume": int(float(rec["v"])), "amount": float(rec["a"]),
                "source": "westock-data.kline"})
        if rows_data:
            store.upsert_daily_price(pd.DataFrame(rows_data))
            written += len(rows_data)
    except: pass
    if (bi+1) % 10 == 0:
        print(f"  {bi+1}/{len(batches)} 批, 写{written}行", flush=True)

final = store.execute(
    "SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [target]
).fetchone()[0]
print(f"\n✅ {target}: {final} 行 (补了 {written})", flush=True)
store.close()
