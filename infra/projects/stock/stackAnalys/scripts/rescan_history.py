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
from src.utils.config import add_db_path_arg, ensure_project_dirs, load_settings, resolve_db_path
from src.utils.logger import setup_logger
from src.utils.proxy import disable_proxy_if_configured


SAMPLE_CODES = ["sz.000001", "sh.600000", "sz.300750"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rescan historical data into the Parquet lake without duplicating primary keys.")
    add_db_path_arg(parser)
    parser.add_argument("--start", required=True, help="Start date, e.g. 2024-01-01.")
    parser.add_argument("--end", required=True, help="End date, e.g. 2024-12-31.")
    parser.add_argument("--codes", default=None, help="Comma-separated ts_code list, e.g. sz.000001,sh.600000.")
    parser.add_argument("--all", action="store_true", help="Rescan all stocks from latest stock_basic snapshot.")
    parser.add_argument("--sample", action="store_true", help="Rescan sample stocks.")
    parser.add_argument("--include-index", action="store_true", help="Also rescan configured broad index daily data.")
    return parser.parse_args()


def _codes_from_args(args: argparse.Namespace, stock_basic: pd.DataFrame) -> list[str]:
    if args.codes:
        return [code.strip() for code in args.codes.split(",") if code.strip()]
    if args.sample or not args.all:
        return SAMPLE_CODES
    return stock_basic["ts_code"].dropna().astype(str).tolist()


def _log_update(store: LakeStore, task_name: str, target: str, status: str, message: str) -> None:
    store.execute(
        "INSERT INTO daily_update_log (log_time, task_name, target, status, message) VALUES (?, ?, ?, ?, ?)",
        [pd.Timestamp.now(), task_name, target, status, message],
    )


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    ensure_project_dirs(settings)
    setup_logger("rescan_history.log")
    provider = MarketDataProvider()
    store = LakeStore(db_path=resolve_db_path(settings, args.db_path))
    sleep_min = float(settings.get("collector", {}).get("request_sleep_min", 0.5))
    sleep_max = float(settings.get("collector", {}).get("request_sleep_max", 1.5))

    try:
        stock_basic = provider.get_stock_basic()
        store.write_stock_basic_snapshot(stock_basic, snapshot_date=args.end)
        for ts_code in _codes_from_args(args, stock_basic):
            try:
                df = provider.get_daily_price(ts_code, args.start, args.end)
                if not df.empty:
                    store.upsert_daily_price(df)
                msg = f"range={args.start}:{args.end} rows={len(df)} source={provider.last_run.source if provider.last_run else ''}"
                _log_update(store, "rescan_daily_price", ts_code, "OK", msg)
                logger.info("rescan {} {}", ts_code, msg)
            except Exception as exc:
                logger.exception("rescan failed for {}", ts_code)
                _log_update(store, "rescan_daily_price", ts_code, "FAIL", repr(exc))
            time.sleep(random.uniform(sleep_min, sleep_max))

        if args.include_index:
            for index_code in ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]:
                try:
                    df = provider.get_index_daily(index_code, args.start, args.end)
                    if not df.empty:
                        store.upsert_index_daily(df)
                    msg = f"range={args.start}:{args.end} rows={len(df)} source={provider.last_run.source if provider.last_run else ''}"
                    _log_update(store, "rescan_index_daily", index_code, "OK", msg)
                    logger.info("rescan index {} {}", index_code, msg)
                except Exception as exc:
                    logger.exception("rescan index failed for {}", index_code)
                    _log_update(store, "rescan_index_daily", index_code, "FAIL", repr(exc))
                time.sleep(random.uniform(sleep_min, sleep_max))
    finally:
        store.close()


if __name__ == "__main__":
    main()
