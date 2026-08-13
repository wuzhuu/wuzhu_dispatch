from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optional AKShare Shenwan industry index backfill.")
    add_db_path_arg(parser)
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and summarize without writing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    db_path = Path(resolve_db_path(settings, args.db_path)).expanduser().resolve(strict=False)
    store = LakeStore(db_path=db_path)
    try:
        store.connect()
        df = fetch_sw_industry_index(args.start, args.end)
        if df.empty:
            print("WARNING: no optional SW industry index rows fetched; local aggregate is unaffected.")
            return
        print(f"optional sw_industry_index_daily rows: {len(df)}")
        print(f"date_range: {df['trade_date'].min()}..{df['trade_date'].max()}")
        if args.dry_run:
            print("dry_run: true")
            return
        written = store.write_sw_industry_index_daily(df)
        print(f"sw_industry_index_daily rows written: {written}")
        print("view refreshed: v_sw_industry_index_daily")
    except Exception as exc:
        print(f"WARNING: optional sw_industry_index_daily failed: {exc!r}; local aggregate is unaffected.")
    finally:
        store.close()


def fetch_sw_industry_index(start: str, end: str) -> pd.DataFrame:
    try:
        import akshare as ak
    except Exception as exc:
        print(f"WARNING: akshare import failed: {exc!r}")
        return pd.DataFrame()

    start_raw = pd.to_datetime(start).strftime("%Y%m%d")
    end_raw = pd.to_datetime(end).strftime("%Y%m%d")
    rows = []
    try:
        names = ak.index_realtime_sw()
    except Exception as exc:
        print(f"WARNING: akshare index_realtime_sw failed: {exc!r}")
        return pd.DataFrame()
    if names is None or names.empty:
        return pd.DataFrame()
    code_col = _first_col(names, ["指数代码", "代码", "index_code", "指数编码"])
    name_col = _first_col(names, ["指数名称", "名称", "index_name"])
    if not code_col or not name_col:
        print("WARNING: AKShare SW realtime schema missing code/name columns.")
        return pd.DataFrame()
    for item in names[[code_col, name_col]].dropna().drop_duplicates().to_dict("records"):
        code = str(item[code_col])
        name = str(item[name_col])
        try:
            hist = ak.index_hist_sw(symbol=code, period="day", start_date=start_raw, end_date=end_raw)
        except Exception as exc:
            print(f"WARNING: optional SW index fetch failed for {code} {name}: {exc!r}")
            continue
        if hist is None or hist.empty:
            continue
        norm = normalize_history(hist, code, name)
        rows.append(norm)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def normalize_history(df: pd.DataFrame, code: str, name: str) -> pd.DataFrame:
    col = {str(c): c for c in df.columns}
    out = pd.DataFrame()
    out["trade_date"] = pd.to_datetime(df[_first_col(df, ["日期", "date", "trade_date"])], errors="coerce").dt.date
    out["industry_code"] = code
    out["industry_name"] = name
    out["open"] = _num(df, col, ["开盘", "open"])
    out["high"] = _num(df, col, ["最高", "high"])
    out["low"] = _num(df, col, ["最低", "low"])
    out["close"] = _num(df, col, ["收盘", "close"])
    out["preclose"] = _num(df, col, ["昨收", "preclose"])
    out["pct_chg"] = _num(df, col, ["涨跌幅", "pct_chg"])
    out["volume"] = _num(df, col, ["成交量", "volume"])
    out["amount"] = _num(df, col, ["成交额", "amount"])
    out["source"] = "akshare.sw_index_optional"
    out["created_at"] = pd.Timestamp.now()
    return out.dropna(subset=["trade_date"])


def _first_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    names = {str(c).lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate.lower() in names:
            return names[candidate.lower()]
    return None


def _num(df: pd.DataFrame, col: dict[str, object], candidates: list[str]) -> pd.Series:
    for candidate in candidates:
        if candidate in col:
            return pd.to_numeric(df[col[candidate]], errors="coerce")
    return pd.Series([pd.NA] * len(df), dtype="Float64")


if __name__ == "__main__":
    main()
