"""
Fast collection script: collect today's sina spot data + index daily,
skipping the slow baostock historical backfill.
Run from PROJECT_ROOT (stackAnalys/).
"""
from __future__ import annotations

import pandas as pd
import asyncio
import sys
from datetime import datetime, date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # stackAnalys/
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_settings, resolve_db_path, ensure_project_dirs
from src.storage.lake_store import LakeStore
from src.collectors.market_provider import MarketDataProvider
from src.utils.logger import setup_logger
from loguru import logger


def main():
    settings = load_settings()
    ensure_project_dirs(settings)
    setup_logger("fast_collect_today.log")

    db_path = resolve_db_path(settings, None)
    store = LakeStore(db_path=db_path)
    provider = MarketDataProvider()

    target_date = date.today().strftime("%Y-%m-%d")
    logger.info("Target date: {}", target_date)

    try:
        # Step 1: Collect stock_basic
        logger.info("=== Step 1: stock_basic ===")
        try:
            stock_basic = provider.get_stock_basic()
            source = provider.last_run.source if provider.last_run else ""
            rows = len(stock_basic)
            if rows > 0:
                store.write_stock_basic_snapshot(stock_basic, snapshot_date=target_date)
                logger.info("stock_basic: {} rows from {}", rows, source)
            else:
                logger.warning("stock_basic returned 0 rows")
        except Exception as e:
            logger.exception("stock_basic failed")

        # Step 2: Collect sina spot daily for today
        logger.info("=== Step 2: sina_spot_daily ===")
        try:
            from src.collectors.sina_spot_daily import (
                fetch_sina_spot_daily_streaming,
                SinaSpotConfig,
                SINA_SOURCE_NAME,
            )
            from src.jobs.daily_update_job import _after_formal_daily_close

            allow_formal_write = _after_formal_daily_close(target_date, "15:10")
            logger.info("Formal close check: allow_formal_write={}", allow_formal_write)

            collector_cfg = settings.get("collector", {})
            config = SinaSpotConfig(
                batch_size=int(collector_cfg.get("sina_batch_size", 80)),
                qps=float(collector_cfg.get("sina_qps", 5)),
                max_concurrency=int(collector_cfg.get("sina_max_concurrency", 10)),
                timeout=float(collector_cfg.get("sina_timeout", 10)),
                retries=int(collector_cfg.get("sina_retries", 2)),
            )

            stock_basic_df = provider.get_stock_basic() if "stock_basic" not in dir() else stock_basic
            ts_codes = stock_basic_df["ts_code"].dropna().astype(str).tolist()
            logger.info("Fetching sina spot for {} stocks", len(ts_codes))

            rows_written = 0
            temp_rows = 0
            missing_rows = 0

            def on_flush(prices, missing, batches):
                nonlocal rows_written, temp_rows, missing_rows
                if not missing.empty:
                    try:
                        store.upsert_missing_daily_price(missing)
                        missing_rows += len(missing)
                    except Exception as e:
                        logger.warning("upsert_missing_daily_price error: {}", e)
                if not prices.empty and allow_formal_write:
                    try:
                        store.upsert_daily_price(prices)
                        rows_written += len(prices)
                    except Exception as e:
                        logger.warning("upsert_daily_price error: {}", e)
                elif not prices.empty:
                    try:
                        store.upsert_daily_price_temp(prices, status="intraday")
                        temp_rows += len(prices)
                    except Exception as e:
                        logger.warning("upsert_daily_price_temp error: {}", e)
                if batches:
                    try:
                        store.upsert_collection_checkpoint(pd.DataFrame(batches))
                    except Exception as e:
                        # Known issue: duplicate checkpoint keys for sys_auth
                        logger.warning("Checkpoint upsert non-fatal: {}", e)

            stats = asyncio.run(
                fetch_sina_spot_daily_streaming(
                    ts_codes,
                    target_date,
                    config,
                    flush_every_batches=int(collector_cfg.get("sina_write_flush_batches", 10)),
                    on_flush=on_flush,
                    skip_batch_keys=set(),
                )
            )
            logger.info(
                "sina_spot_daily done: {} rows_written={} temp_rows={} missing_rows={} "
                "success_rate={:.4f} suspended={}",
                stats.get("success_count", 0), rows_written, temp_rows, missing_rows,
                float(stats.get("success_rate", 0)),
                int(stats.get("suspended_count", 0)),
            )
        except Exception as e:
            logger.exception("sina_spot_daily failed")

        # Step 3: Collect index daily
        logger.info("=== Step 3: index_daily ===")
        try:
            INDEX_CODES = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]
            index_rows = 0
            for idx_code in INDEX_CODES:
                try:
                    df = provider.get_index_daily(idx_code, target_date, target_date)
                    if not df.empty:
                        store.upsert_index_daily(df)
                        index_rows += len(df)
                    logger.info("index_daily {}: {} rows", idx_code, len(df))
                except Exception as e:
                    logger.warning("index_daily {} failed: {}", idx_code, e)
            logger.info("index_daily total: {} rows", index_rows)
        except Exception as e:
            logger.exception("index_daily failed")

        # Step 4: Run quality checks
        logger.info("=== Step 4: Quality checks ===")
        try:
            from src.jobs.quality_check import run_quality_checks
            quality_df = run_quality_checks(store, target_date)
            if quality_df is not None and not quality_df.empty:
                logger.info("Quality checks passed: {} rows", len(quality_df))
            else:
                logger.info("No quality issues")
        except Exception as e:
            logger.warning("Quality checks error: {}", e)

        # Verification
        logger.info("=== Verification ===")
        for tbl in ["daily_price", "index_daily"]:
            try:
                row = store.execute(f"SELECT COUNT(*) FROM {tbl} WHERE trade_date = ?", [target_date]).fetchone()
                logger.info("{} for {}: {} rows", tbl, target_date, row[0] if row else 0)
            except Exception as e:
                logger.warning("{} check failed: {}", tbl, e)

        logger.info("=== Collection complete ===")

    finally:
        provider.close_baostock_session()
        store.close()


if __name__ == "__main__":
    main()
