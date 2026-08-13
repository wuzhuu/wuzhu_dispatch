from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import pandas as pd
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.market_provider import MarketDataProvider
from src.storage.lake_store import LakeStore
from src.utils.config import ensure_project_dirs, load_settings, add_db_path_arg, resolve_db_path
from src.utils.logger import setup_logger
from src.utils.proxy import disable_proxy_if_configured


SAMPLE_CODES = ["sz.000001", "sh.600000", "sz.300750"]
INDEX_CODES = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]


def _log_update(store: LakeStore, task_name: str, target: str, status: str, message: str) -> None:
    store.execute(
        "INSERT INTO daily_update_log (log_time, task_name, target, status, message) VALUES (?, ?, ?, ?, ?)",
        [pd.Timestamp.now(), task_name, target, status, message],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill daily A-share data with BaoStock primary and AKShare fallback.")
    add_db_path_arg(parser)
    parser.add_argument("--sample", action="store_true", help="Only backfill 000001, 600000, 300750.")
    parser.add_argument("--start", default=None, help="Start date, e.g. 2025-01-01.")
    parser.add_argument("--end", default=None, help="End date, e.g. 2026-05-31.")
    parser.add_argument("--all", action="store_true", help="Backfill all stocks from stock_basic.")
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    ensure_project_dirs(settings)
    setup_logger("backfill_history.log")
    provider = MarketDataProvider()
    store = LakeStore(db_path=resolve_db_path(settings, args.db_path))
    start = args.start or settings.get("collector", {}).get("backfill_start_date", "2018-01-01")
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    sleep_min = float(settings.get("collector", {}).get("request_sleep_min", 0.5))
    sleep_max = float(settings.get("collector", {}).get("request_sleep_max", 1.5))

    try:
        stock_basic = provider.get_stock_basic()
        store.write_stock_basic_snapshot(stock_basic, snapshot_date=end)
        if args.sample or not args.all:
            codes = SAMPLE_CODES
        else:
            codes = stock_basic["ts_code"].dropna().astype(str).tolist()

        for ts_code in codes:
            try:
                df = provider.get_daily_price(ts_code, start, end)
                if not df.empty:
                    store.upsert_daily_price(df)
                msg = f"rows={len(df)} source={provider.last_run.source if provider.last_run else ''} fallback={provider.last_run.fallback_used if provider.last_run else ''}"
                _log_update(store, "backfill_daily_price", ts_code, "OK", msg)
                logger.info("backfill {} {}", ts_code, msg)
            except Exception as exc:
                logger.exception("backfill failed for {}", ts_code)
                _log_update(store, "backfill_daily_price", ts_code, "FAIL", repr(exc))
            time.sleep(random.uniform(sleep_min, sleep_max))

        for index_code in INDEX_CODES:
            try:
                df = provider.get_index_daily(index_code, start, end)
                if not df.empty:
                    store.upsert_index_daily(df)
                msg = f"rows={len(df)} source={provider.last_run.source if provider.last_run else ''} fallback={provider.last_run.fallback_used if provider.last_run else ''}"
                _log_update(store, "backfill_index_daily", index_code, "OK", msg)
                logger.info("backfill index {} {}", index_code, msg)
            except Exception as exc:
                logger.exception("backfill index failed for {}", index_code)
                _log_update(store, "backfill_index_daily", index_code, "FAIL", repr(exc))
            time.sleep(random.uniform(sleep_min, sleep_max))
    finally:
        store.close()


if __name__ == "__main__":
    main()
