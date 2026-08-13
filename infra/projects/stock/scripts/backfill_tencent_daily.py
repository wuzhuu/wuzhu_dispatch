"""
Recovery backfill: collect daily_price for a single target date via the tencent source,
bypassing baostock (which has a bug-2 socket-hang regression making it hang/unreliable for bulk).
Run from stackAnalys/. Schema matches the pipeline (uses MarketDataProvider + LakeStore).
"""
from __future__ import annotations
import sys, time, concurrent.futures
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/home/stock/stock/stackAnalys")
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_settings, resolve_db_path
from src.storage.lake_store import LakeStore
from src.collectors.market_provider import MarketDataProvider
from src.utils.logger import setup_logger
from loguru import logger

TARGET = "2026-08-04"
WORKERS = 6
WRITE_BATCH = 200


def _normalize_provider_df(ts_code, df):
    if df is None or df.empty:
        return None
    # ensure ts_code / trade_date columns present like pipeline frames
    if "source" not in df.columns:
        df = df.copy()
        df["source"] = "tencent"
    return df


def main():
    settings = load_settings()
    setup_logger("backfill_tencent.log")
    db_path = resolve_db_path(settings, None)
    store = LakeStore(db_path=db_path)
    provider = MarketDataProvider()

    # Load all ts_codes from stock_basic 08-04 snapshot
    rows = store.execute(
        "SELECT DISTINCT ts_code FROM stock_basic WHERE snapshot_date = ? AND ts_code IS NOT NULL",
        [TARGET],
    ).fetchall()
    ts_codes = [r[0] for r in rows]
    logger.info("Loaded {} ts_codes from stock_basic snapshot {}", len(ts_codes), TARGET)

    frames: list = []
    total = 0
    failed = []

    def flush():
        nonlocal frames, total
        if not frames:
            return
        try:
            rows_written = store.upsert_daily_price(_concat(frames))
            total += rows_written
            logger.info("flushed {} frames -> {} rows", len(frames), rows_written)
        except Exception as e:
            logger.warning("flush failed: {}", e)
        frames = []

    def _concat(fl):
        import pandas as pd
        return pd.concat(fl, ignore_index=True)

    def fetch_one(ts_code):
        try:
            df = provider.get_daily_price_multi_source(ts_code, TARGET, TARGET, ["tencent"])
            if df is not None and not df.empty:
                return df, None
            return None, f"empty:{ts_code}"
        except Exception as exc:
            return None, f"{ts_code}:{type(exc).__name__}:{str(exc)[:40]}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, c): c for c in ts_codes}
        done_cnt = 0
        for fut in concurrent.futures.as_completed(futs):
            code = futs[fut]
            df, err = fut.result()
            done_cnt += 1
            if df is not None:
                frames.append(df)
                if len(frames) >= WRITE_BATCH:
                    flush()
            elif err:
                # empty is normal (suspended/delisted/untraded); hard errors tracked
                if not err.startswith("empty:"):
                    failed.append(err)
            if done_cnt % 500 == 0:
                logger.info("progress {}/{}", done_cnt, len(ts_codes))

    flush()

    logger.info("=== backfill complete ===")
    logger.info("total rows written: {}", total)
    logger.info("hard failures ({}): {}", len(failed), failed[:20])

    # index_daily via sina for target
    INDEX_CODES = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]
    idx_rows = 0
    for ic in INDEX_CODES:
        try:
            d = provider.get_index_daily(ic, TARGET, TARGET)
            if not d.empty:
                store.upsert_index_daily(d)
                idx_rows += len(d)
            logger.info("index_daily {}: {} rows", ic, len(d))
        except Exception as e:
            logger.warning("index_daily {} failed: {}", ic, e)
    logger.info("index_daily total: {}", idx_rows)

    # verify
    for tbl in ["daily_price", "index_daily"]:
        try:
            n = store.execute(f"SELECT COUNT(*) FROM {tbl} WHERE trade_date = ?", [TARGET]).fetchone()[0]
            logger.info("{} {} rows: {}", tbl, TARGET, n)
        except Exception as e:
            logger.warning("{} verify failed: {}", tbl, e)

    provider.close_baostock_session()
    store.close()
    print(f"BACKFILL_DONE daily_price_rows={total} index_rows={idx_rows} failures={len(failed)}")


if __name__ == "__main__":
    main()