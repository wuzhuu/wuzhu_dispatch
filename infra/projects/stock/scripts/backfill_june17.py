#!/usr/bin/env python3
"""Backfill missing June 17 daily_price data via baostock."""

import sys
from pathlib import Path

PROJECT_ROOT = Path("/home/stock/stock/stackAnalys")
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.daily_update_job import run_daily_update
from src.utils.config import resolve_db_path, load_settings

settings = load_settings()
db_path = resolve_db_path(settings)

# Override settings to disable sina_spot_daily and use baostock directly
# This ensures history_end_date = target_date (not target_date - 1)
overrides = {
    "data_source": {
        "daily_price_current_primary": "baostock",
    }
}

print(f"Running backfill for 2026-06-17 with baostock primary...")
run_daily_update(
    target_date="2026-06-17",
    db_path=str(db_path),
    dry_run=False,
    settings_overrides=overrides,
)
print("June 17 backfill completed.")
