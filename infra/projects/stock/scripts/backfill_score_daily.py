#!/usr/bin/env python3
"""
回采 score_daily：对指定日期重新评分。
用 DuckDB SQL 直接计算每个股票的最新因子（到指定日期为止），
然后用 score_stocks() 做排名。
"""
import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "stackAnalys"
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.scoring import score_stocks
from src.analysis.risk import add_risk_flags
from src.utils.config import load_settings, resolve_db_path


def compute_factors_fast(conn: duckdb.DuckDBPyConnection, target_date: str) -> pd.DataFrame:
    """
    用单个 SQL 查询计算指定日期为止各股票的最新因子。
    相当于 calc_latest_factors() 的加速版——直接在 DuckDB 里聚合。
    """
    sql = f"""
    WITH price_win AS (
        SELECT ts_code, trade_date, close, amount, pct_chg, source,
               -- 滚动指标窗口
               AVG(close) OVER w20 as ma20,
               AVG(close) OVER w60 as ma60,
               STDDEV_SAMP(pct_chg) OVER w20 * SQRT(252) as volatility_20d,
               MAX(close) OVER w60 as peak_60d,
               AVG(amount) OVER w20 as amount_ma20,
               ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn,
               COUNT(*) OVER (PARTITION BY ts_code) as history_count
        FROM v_daily_price
        WHERE trade_date BETWEEN '2025-01-01' AND '{target_date}'
          AND (adjust_type = 'qfq' OR adjust_type IS NULL)
          AND close IS NOT NULL AND close > 0
        WINDOW 
            w20 AS (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
            w60 AS (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
    ),
    latest AS (
        SELECT ts_code, trade_date, close, amount, source,
               ma20, ma60, volatility_20d, peak_60d, amount_ma20, history_count
        FROM price_win WHERE rn = 1
    ),
    momentum AS (
        SELECT ts_code,
               -- N天前收盘价
               FIRST_VALUE(close) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) as close_20d_ago,
               FIRST_VALUE(close) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW) as close_60d_ago,
               FIRST_VALUE(close) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 120 PRECEDING AND CURRENT ROW) as close_120d_ago
        FROM v_daily_price
        WHERE trade_date BETWEEN '2025-01-01' AND '{target_date}'
          AND (adjust_type = 'qfq' OR adjust_type IS NULL)
          AND close IS NOT NULL AND close > 0
    )
    SELECT l.*,
           (l.close / m20.close_20d_ago - 1) as momentum_20d,
           (l.close / m60.close_60d_ago - 1) as momentum_60d,
           (l.close / m120.close_120d_ago - 1) as momentum_120d,
           (l.close / l.peak_60d - 1) as drawdown_60d,
           (l.close > l.ma20) as trend_ma20,
           (l.close > l.ma60) as trend_ma60
    FROM latest l
    LEFT JOIN momentum m20 ON l.ts_code = m20.ts_code
    LEFT JOIN momentum m60 ON l.ts_code = m60.ts_code
    LEFT JOIN momentum m120 ON l.ts_code = m120.ts_code
    ORDER BY l.ts_code
    """
    return conn.execute(sql).df()


def main():
    parser = argparse.ArgumentParser(description="Backfill score_daily for specific dates")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dates", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    db_path = args.db_path or resolve_db_path(settings)
    conn = duckdb.connect(str(db_path))
    store = AnalysisLakeStore(db_path)

    # Detect affected dates
    if args.dates:
        affected = args.dates
    else:
        print("Auto-detecting incomplete dates...")
        rows = conn.execute("""
            SELECT trade_date,
                   COUNT(CASE WHEN ts_code LIKE 'sh.%' THEN 1 END) as sh,
                   COUNT(CASE WHEN ts_code LIKE 'sz.%' THEN 1 END) as sz
            FROM v_score_daily
            GROUP BY trade_date
            ORDER BY trade_date
        """).fetchall()
        affected = []
        for r in rows:
            ds = str(r[0])[:10]
            sh, sz = r[1], r[2]
            if sh < 1000 or sz < 1000:
                print(f"  {ds}: SH={sh} SZ={sz} ***")
                affected.append(ds)

    for d in ["2026-05-29", "2026-06-01", "2026-06-02", "2026-06-10"]:
        if d not in affected:
            affected.append(d)

    if not affected:
        print("No incomplete dates found.")
        conn.close()
        store.close()
        return

    # Also fetch stock basic info for merging
    basic = conn.execute("SELECT ts_code, name, industry, market FROM stock_basic").df()

    print(f"\nTarget dates: {sorted(affected)}")
    print(f"Dry run: {args.dry_run}\n")

    for target_date in sorted(affected):
        print(f"{'='*60}")
        print(f"Date: {target_date}")

        cur = conn.execute("""
            SELECT COUNT(*) as total,
                   COUNT(CASE WHEN ts_code LIKE 'sh.%' THEN 1 END) as sh,
                   COUNT(CASE WHEN ts_code LIKE 'sz.%' THEN 1 END) as sz
            FROM v_score_daily
            WHERE trade_date = ?::TIMESTAMP
        """, [target_date]).fetchone()
        print(f"  Before: total={cur[0]} SH={cur[1]} SZ={cur[2]}")

        # Compute factors
        print(f"  Computing factors...", end=" ", flush=True)
        df = compute_factors_fast(conn, target_date)
        if df.empty:
            print(f"SKIP: no price data")
            continue
        print(f"{len(df)} stocks")

        # Clean up columns
        df["trade_date"] = pd.to_datetime(target_date)
        for col in ["trend_ma20", "trend_ma60"]:
            if col in df.columns:
                df[col] = df[col].fillna(False).astype(bool)
        for col in ["momentum_20d", "momentum_60d", "momentum_120d"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Score
        print(f"  Scoring...", end=" ", flush=True)
        require_cols = ["ts_code", "trade_date", "close",
                        "momentum_20d", "momentum_60d", "momentum_120d",
                        "volatility_20d", "amount_ma20", "drawdown_60d",
                        "trend_ma20", "trend_ma60", "history_count", "source"]
        available = [c for c in require_cols if c in df.columns]
        enriched = df[available].copy()
        enriched = enriched.rename(columns={"amount_ma20": "amount_ma_20"})
        ranked = score_stocks(enriched)
        ranked = add_risk_flags(ranked)

        # Merge basic info
        keep = [c for c in ["ts_code", "name", "industry", "market"] if c in basic.columns]
        if keep:
            ranked = ranked.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left")

        sh_new = len(ranked[ranked["ts_code"].str.startswith("sh.", na=False)])
        sz_new = len(ranked[ranked["ts_code"].str.startswith("sz.", na=False)])
        print(f"{len(ranked)} scored (SH={sh_new} SZ={sz_new})")

        if args.dry_run:
            print(f"  [DRY RUN - skip write]")
            continue

        # Write
        print(f"  Writing to score_daily...", end=" ", flush=True)
        written = store.write_dataset("score_daily", ranked)
        print(f"{len(written)} partitions")

        # Verify
        after = conn.execute("""
            SELECT COUNT(*) as total,
                   COUNT(CASE WHEN ts_code LIKE 'sh.%' THEN 1 END) as sh,
                   COUNT(CASE WHEN ts_code LIKE 'sz.%' THEN 1 END) as sz
            FROM v_score_daily
            WHERE trade_date = ?::TIMESTAMP
        """, [target_date]).fetchone()
        print(f"  After: total={after[0]} SH={after[1]} SZ={after[2]}")

    conn.close()
    store.close()
    print(f"\n{'='*60} DONE {'='*60}")


if __name__ == "__main__":
    main()
