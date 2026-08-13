from __future__ import annotations

import pandas as pd


INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000905.SH", "000852.SH"]


def _normalize_index(df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rename_map = {"日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "vol", "成交额": "amount", "涨跌幅": "pct_chg", "涨跌额": "change"}
    out = df.rename(columns=rename_map).copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y%m%d")
    out["index_code"] = index_code
    out["pre_close"] = None
    out["source"] = "akshare.index_zh_a_hist"
    out["updated_at"] = pd.Timestamp.now()
    return out[["index_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount", "source", "updated_at"]]


def fetch_index_daily(trade_date: str) -> pd.DataFrame:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    for index_code in INDEX_CODES:
        symbol = index_code.split(".")[0]
        try:
            df = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=trade_date, end_date=trade_date)
            normalized = _normalize_index(df, index_code)
            if not normalized.empty:
                frames.append(normalized)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
