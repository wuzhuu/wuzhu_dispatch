"""Generate the daily report markdown for today (same format as run_daily.py's _write_daily_report).
Run from PROJECT_ROOT (stackAnalys/)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import resolve_db_path, load_settings, resolve_log_root
from src.utils.logger import setup_logger
from src.jobs.quality_check import run_quality_checks
from loguru import logger
import pandas as pd


def main():
    settings = load_settings()
    target_date = datetime.now().strftime("%Y-%m-%d")
    db_path = resolve_db_path(settings, None)
    store = LakeStore(db_path=db_path)
    report_rows = []

    # Gather update stats
    for label, query in [
        ("daily_price", "SELECT COUNT(*) FROM daily_price WHERE trade_date = ?"),
        ("index_daily", "SELECT COUNT(*) FROM index_daily WHERE trade_date = ?"),
    ]:
        try:
            row = store.execute(query, [target_date]).fetchone()
            report_rows.append(f"- {label} rows: {row[0] if row else 0}")
        except Exception as e:
            report_rows.append(f"- {label}: {e}")

    try:
        sb = store.execute("SELECT COUNT(*) FROM v_stock_basic_latest").fetchone()
        report_rows.append(f"- stock_basic rows: {sb[0] if sb else 0}")
    except Exception as e:
        report_rows.append(f"- stock_basic: {e}")

    try:
        sd = store.execute("SELECT MAX(trade_date), COUNT(*) FROM v_score_daily").fetchone()
        if sd and sd[0]:
            report_rows.append(f"- score_daily date: {sd[0]} rows: {sd[1]}")
    except Exception as e:
        report_rows.append(f"- score_daily: {e}")

    # Quality checks
    quality_df = run_quality_checks(store, target_date)

    # Write the report
    log_root = resolve_log_root(settings)
    log_root.mkdir(parents=True, exist_ok=True)
    path = log_root / f"daily_report_{target_date.replace('-', '')}.md"

    lines = [
        "# Daily Update Report",
        "",
        f"- target_date: {target_date}",
        f"- generated_at: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "## Updates",
        "",
        *report_rows,
        "",
        "## Quality",
        "",
        quality_df.to_markdown(index=False) if quality_df is not None and not quality_df.empty else "No quality rows.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Daily report written: {}", path)
    print(f"Report written: {path}")
    store.close()


if __name__ == "__main__":
    main()
