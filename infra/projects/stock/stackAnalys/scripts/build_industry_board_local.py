from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local industry_board from stock_industry_map and daily_price.")
    add_db_path_arg(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="Build one trade date, YYYY-MM-DD.")
    group.add_argument("--latest", action="store_true", help="Build the latest available trade date.")
    parser.add_argument("--start", help="Start trade date, YYYY-MM-DD.")
    parser.add_argument("--end", help="End trade date, YYYY-MM-DD.")
    parser.add_argument("--adjust-type", default="qfq", help="Preferred daily_price adjust_type. Default: qfq.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing parquet.")
    parser.add_argument("--min-members-warning", type=int, default=3, help="Warn when an industry has fewer valid members.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    db_path = Path(resolve_db_path(settings, args.db_path)).expanduser().resolve(strict=False)
    store = LakeStore(db_path=db_path)
    try:
        store.connect()
        store.refresh_views()
        start, end = resolve_dates(store, args)
        if not start or not end:
            print("WARNING: no trade dates available in daily_price.")
            return
        board, summary, warnings = build_industry_board(store, start, end, args.adjust_type, args.min_members_warning)
        for line in warnings:
            print(f"WARNING: {line}")
        if summary.empty:
            print("WARNING: no industry board rows generated.")
            return
        print(summary.to_string(index=False))
        print(f"rows: {len(board)}")
        print(f"lake_root: {store.lake_root}")
        if args.dry_run:
            print("dry_run: true")
            return
        written = store.write_industry_board_local(board)
        print(f"industry_board_local rows written: {written}")
        print("view refreshed: v_industry_board")
    finally:
        store.close()


def resolve_dates(store: LakeStore, args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.latest:
        row = store.execute("SELECT MAX(trade_date) FROM v_daily_price").fetchone()
        if not row or row[0] is None:
            return None, None
        latest = str(pd.to_datetime(row[0]).date())
        return latest, latest
    if args.date:
        return args.date, args.date
    if args.start and args.end:
        return args.start, args.end
    row = store.execute("SELECT MIN(trade_date), MAX(trade_date) FROM v_daily_price").fetchone()
    if not row or row[0] is None or row[1] is None:
        return None, None
    return str(pd.to_datetime(row[0]).date()), str(pd.to_datetime(row[1]).date())


def build_industry_board(
    store: LakeStore,
    start: str,
    end: str,
    adjust_type: str,
    min_members_warning: int,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    price = load_price(store, start, end, adjust_type)
    if price.empty:
        return pd.DataFrame(), pd.DataFrame(), ["daily_price returned no rows for requested range"]
    industry_map = load_industry_map(store)
    if industry_map.empty:
        return pd.DataFrame(), pd.DataFrame(), ["v_stock_industry_map is empty; cannot aggregate mapped industries"]

    price = compute_returns(price)
    window = price[(price["trade_date"] >= pd.to_datetime(start)) & (price["trade_date"] <= pd.to_datetime(end))].copy()
    merged = window.merge(industry_map, on="ts_code", how="left")
    merged["mapped"] = merged["industry_name"].notna()
    unmapped_by_date = merged.groupby("trade_date")["mapped"].apply(lambda s: int((~s).sum())).rename("unmapped_count")
    total_by_date = merged.groupby("trade_date")["ts_code"].nunique().rename("total_stock_count")
    mapped = merged[merged["mapped"]].copy()
    if mapped.empty:
        return pd.DataFrame(), pd.DataFrame(), ["all daily_price rows are missing industry mapping"]

    group_cols = ["trade_date", "industry_source", "industry_level", "industry_code", "industry_name"]
    rows = (
        mapped.groupby(group_cols, dropna=False)
        .agg(
            member_count=("ts_code", "nunique"),
            valid_member_count=("ret_1d", lambda s: int(s.notna().sum())),
            up_count=("ret_1d", lambda s: int((s > 0).sum())),
            down_count=("ret_1d", lambda s: int((s < 0).sum())),
            flat_count=("ret_1d", lambda s: int((s == 0).sum())),
            avg_ret_1d=("ret_1d", "mean"),
            median_ret_1d=("ret_1d", "median"),
            avg_ret_5d=("ret_5d", "mean"),
            median_ret_5d=("ret_5d", "median"),
            avg_ret_20d=("ret_20d", "mean"),
            median_ret_20d=("ret_20d", "median"),
            amount_sum=("amount", "sum"),
            volume_sum=("volume", "sum"),
        )
        .reset_index()
    )
    rows = rows.merge(unmapped_by_date.reset_index(), on="trade_date", how="left")
    rows["unmapped_count"] = rows["unmapped_count"].fillna(0).astype("int64")
    rows["breadth_up_ratio"] = rows["up_count"] / rows["valid_member_count"].replace(0, np.nan)
    rows = rows.sort_values(["industry_source", "industry_level", "industry_code", "trade_date"])
    rows["amount_ma20"] = rows.groupby(["industry_source", "industry_level", "industry_code"])["amount_sum"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    rows = add_scores(rows)
    rows["source"] = "local_aggregate"
    rows["created_at"] = pd.Timestamp.now()
    rows["data_quality_flags"] = rows.apply(_quality_flags, axis=1)

    warnings = []
    small = rows[rows["valid_member_count"] < min_members_warning]
    if not small.empty:
        warnings.append(f"{len(small)} industry/date rows have valid_member_count < {min_members_warning}")
    if rows["avg_ret_5d"].isna().any():
        warnings.append("some avg_ret_5d values are NULL because the lookback window is insufficient")
    if rows["avg_ret_20d"].isna().any():
        warnings.append("some avg_ret_20d/amount_ma20 values are NULL because the lookback window is insufficient")

    summary = (
        rows.groupby("trade_date")
        .agg(industry_count=("industry_name", "nunique"), valid_member_count=("valid_member_count", "sum"), unmapped_count=("unmapped_count", "max"))
        .reset_index()
        .merge(total_by_date.reset_index(), on="trade_date", how="left")
    )
    summary["coverage_rate"] = summary["valid_member_count"] / summary["total_stock_count"].replace(0, np.nan)
    summary["missing_rate"] = summary["unmapped_count"] / summary["total_stock_count"].replace(0, np.nan)
    summary["trade_date"] = pd.to_datetime(summary["trade_date"]).dt.strftime("%Y-%m-%d")
    return rows, summary, warnings


def load_price(store: LakeStore, start: str, end: str, adjust_type: str) -> pd.DataFrame:
    lookback_start = (pd.to_datetime(start) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    cols = store.execute("DESCRIBE SELECT * FROM v_daily_price LIMIT 0").df()["column_name"].astype(str).tolist()
    adjust_filter = ""
    params: list[object] = [lookback_start, end]
    if "adjust_type" in cols:
        preferred_count = store.execute(
            "SELECT COUNT(*) FROM v_daily_price WHERE adjust_type = ? AND trade_date BETWEEN ? AND ?",
            [adjust_type, lookback_start, end],
        ).fetchone()[0]
        if preferred_count:
            adjust_filter = "AND adjust_type = ?"
            params.append(adjust_type)
    return store.execute(
        f"""
        SELECT ts_code, trade_date, close, preclose, volume, amount, pct_chg, adjust_type, source_priority, updated_at
        FROM v_daily_price
        WHERE trade_date BETWEEN ? AND ?
        {adjust_filter}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ts_code, trade_date, adjust_type
            ORDER BY source_priority DESC NULLS LAST, updated_at DESC NULLS LAST
        ) = 1
        """,
        params,
    ).df()


def load_industry_map(store: LakeStore) -> pd.DataFrame:
    try:
        df = store.execute(
            """
            SELECT
                ts_code,
                COALESCE(industry_source, source, 'unknown') AS industry_source,
                CASE
                    WHEN industry_name_l3 IS NOT NULL THEN 'l3'
                    WHEN industry_name_l2 IS NOT NULL THEN 'l2'
                    ELSE 'l1'
                END AS industry_level,
                COALESCE(industry_code_l3, industry_code_l2, industry_code_l1, sw_code_2021, industry_name) AS industry_code,
                COALESCE(industry_name_l3, industry_name_l2, industry_name_l1, industry_name) AS industry_name
            FROM v_stock_industry_map
            WHERE industry_name IS NOT NULL
            """
        ).df()
    except Exception:
        return pd.DataFrame()
    return df.dropna(subset=["ts_code", "industry_name"]).drop_duplicates("ts_code")


def compute_returns(price: pd.DataFrame) -> pd.DataFrame:
    out = price.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    for col in ("close", "preclose", "volume", "amount", "pct_chg"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.sort_values(["ts_code", "trade_date"])
    prev_close = out.groupby("ts_code")["close"].shift(1)
    out["ret_1d"] = out["close"] / prev_close - 1
    if "pct_chg" in out and out["ret_1d"].isna().any():
        out["ret_1d"] = out["ret_1d"].fillna(out["pct_chg"] / 100.0)
    out["ret_5d"] = out["close"] / out.groupby("ts_code")["close"].shift(5) - 1
    out["ret_20d"] = out["close"] / out.groupby("ts_code")["close"].shift(20) - 1
    return out


def add_scores(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    scored = []
    for _, part in out.groupby("trade_date", sort=False):
        part = part.copy()
        part["industry_momentum_score"] = _zscore(part["avg_ret_20d"].fillna(part["avg_ret_5d"]).fillna(part["avg_ret_1d"]))
        part["industry_breadth_score"] = _zscore(part["breadth_up_ratio"])
        part["industry_liquidity_score"] = _zscore(np.log1p(pd.to_numeric(part["amount_ma20"], errors="coerce")))
        score_cols = ["industry_momentum_score", "industry_breadth_score", "industry_liquidity_score"]
        available = part[score_cols].notna()
        weighted = (
            part["industry_momentum_score"] * 0.4
            + part["industry_breadth_score"] * 0.3
            + part["industry_liquidity_score"] * 0.3
        )
        part["industry_strength_score"] = weighted.where(available.all(axis=1))
        scored.append(part)
    return pd.concat(scored, ignore_index=True) if scored else out


def _zscore(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    std = numeric.std(skipna=True)
    if pd.isna(std) or std == 0:
        return pd.Series([pd.NA] * len(numeric), index=numeric.index, dtype="Float64")
    return (numeric - numeric.mean(skipna=True)) / std


def _quality_flags(row: pd.Series) -> str:
    flags = []
    if pd.isna(row.get("avg_ret_5d")):
        flags.append("missing_ret_5d")
    if pd.isna(row.get("avg_ret_20d")):
        flags.append("missing_ret_20d")
    if pd.isna(row.get("amount_ma20")):
        flags.append("missing_amount_ma20")
    if pd.isna(row.get("industry_strength_score")):
        flags.append("missing_strength_score")
    return ",".join(flags)


if __name__ == "__main__":
    main()
