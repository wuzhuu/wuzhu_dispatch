from __future__ import annotations

import pandas as pd
from datetime import datetime, timedelta


def _yesterday_str() -> str:
    """Return yesterday's date string (YYYY-MM-DD) for baostock queries that
    may run before the current trading day starts (e.g. after midnight)."""
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _last_trade_day_str() -> str:
    """Return the most recent completed trading day (YYYY-MM-DD).

    Unlike calendar yesterday, this skips weekends and holidays: baostock's
    query_all_stock() returns 0 rows for non-trading days, so using calendar
    yesterday breaks every Monday run (yesterday = Sunday) and after holidays.
    """
    from src.utils.calendar import is_trade_day

    d = datetime.now()
    for _ in range(15):
        d -= timedelta(days=1)
        if is_trade_day(d.strftime("%Y%m%d")):
            return d.strftime("%Y-%m-%d")
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _suffix_ts_code(symbol: str) -> tuple[str, str]:
    symbol = str(symbol).zfill(6)
    if symbol.startswith("6"):
        return f"{symbol}.SH", "SSE"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ", "SZSE"
    if symbol.startswith(("4", "8", "9")):
        return f"{symbol}.BJ", "BSE"
    return symbol, ""


def fetch_stock_basic() -> pd.DataFrame:
    """Fetch A-share code/name list through AKShare, no token required."""
    import akshare as ak

    df = ak.stock_info_a_code_name()
    if df.empty:
        return df

    code_col = "code" if "code" in df.columns else "代码"
    name_col = "name" if "name" in df.columns else "名称"
    rows = []
    for _, row in df.iterrows():
        ts_code, exchange = _suffix_ts_code(str(row[code_col]))
        rows.append(
            {
                "ts_code": ts_code,
                "symbol": str(row[code_col]).zfill(6),
                "name": row[name_col],
                "area": None,
                "industry": None,
                "market": "主板/创业板/北交所",
                "list_date": pd.NaT,
                "exchange": exchange,
                "is_hs": None,
                "updated_at": pd.Timestamp.now(),
            }
        )
    return pd.DataFrame(rows)


def fetch_stock_basic_baostock(trade_date: str | None = None) -> pd.DataFrame:
    """Fallback stock list through BaoStock, no token required."""
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        rs = bs.query_all_stock(day=_last_trade_day_str())
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query_all_stock failed: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        raw = pd.DataFrame(rows, columns=rs.fields)
        if raw.empty:
            return raw
        raw["symbol"] = raw["code"].str.split(".").str[1]
        raw["ts_code"] = raw["symbol"].map(lambda x: _suffix_ts_code(x)[0])
        raw["exchange"] = raw["code"].str.split(".").str[0].str.upper().map({"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"})
        raw["updated_at"] = pd.Timestamp.now()
        raw["area"] = None
        raw["industry"] = None
        raw["market"] = None
        raw["list_date"] = pd.NaT
        raw["is_hs"] = None
        raw["name"] = None
        return raw[["ts_code", "symbol", "name", "area", "industry", "market", "list_date", "exchange", "is_hs", "updated_at"]]
    finally:
        bs.logout()
