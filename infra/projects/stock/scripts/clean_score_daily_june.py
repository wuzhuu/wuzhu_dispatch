#!/usr/bin/env python3
"""Clean incomplete dates from June score_daily parquet."""
import shutil
from pathlib import Path
import pandas as pd

lake_root = Path("stock_local_ai_data/lake")
score_dir = lake_root / "score_daily" / "year=2026" / "month=06"
backup_dir = lake_root / "backup"

files = list(score_dir.glob("*.parquet"))
if not files:
    print("No June parquet found")
    exit(1)

pf = files[0]
print(f"Found: {pf} ({pf.stat().st_size / 1024:.0f} KB)")

df = pd.read_parquet(pf)
print(f"Total rows before: {len(df)}")

# Find bad dates
bad_dates = set()
for d in df["trade_date"].unique():
    sub = df[df["trade_date"] == d]
    sh = len(sub[sub["ts_code"].str.startswith("sh.", na=False)])
    sz = len(sub[sub["ts_code"].str.startswith("sz.", na=False)])
    flag = " ***" if (sh < 1000 or sz < 1000) else ""
    print(f"  {d}: total={len(sub)} SH={sh} SZ={sz}{flag}")
    if sh < 1000 or sz < 1000:
        bad_dates.add(d)

print(f"\nDates to remove: {sorted(bad_dates)}")
df_clean = df[~df["trade_date"].isin(bad_dates)]
print(f"Rows after cleanup: {len(df_clean)}")

# Backup
backup_dir.mkdir(parents=True, exist_ok=True)
backup_path = backup_dir / "score_daily_june_backup.pq"
shutil.copy2(pf, backup_path)
print(f"Backup saved: {backup_path}")

# Rewrite
pf.unlink()
df_clean.to_parquet(pf, index=False)
print(f"Written: {pf} ({len(df_clean)} rows)")
