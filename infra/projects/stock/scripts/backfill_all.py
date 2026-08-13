#!/usr/bin/env python3
"""Backfill missing data for June 17 and June 22 in one pass.

Runs daily_update with sina_spot disabled so:
- history_end_date = target_date (full range)
- Baostock fills all gaps from max_date to target_date
- One pass covers both June 17 and June 22 gaps
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path("/home/stock/stock/stackAnalys")
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.daily_update_job import run_daily_update
from src.utils.config import load_settings, resolve_db_path

settings = load_settings()
db_path = resolve_db_path(settings)
print(f"DB path: {db_path}")

# Override: disable sina_spot, use baostock as primary
# This ensures history_end_date = target_date (not previous_day)
overrides = {
    "data_source": {
        "daily_price_current_primary": "baostock",
    }
}

print("=" * 60)
print("Backfill: 2026-06-22 (covers both June 17 and June 22 gaps)")
print(f"DB: {db_path}")
print("Strategy: baostock primary, fetch from max_date to target_date")
print("=" * 60)

try:
    run_daily_update(
        target_date="2026-06-22",
        db_path=str(db_path),
        dry_run=False,
        settings_overrides=overrides,
    )
    print("Backfill completed successfully!")
except Exception as e:
    print(f"Backfill failed: {e}")
    raise
