from __future__ import annotations

import pandas as pd


def fetch_financial_indicators_akshare(ts_code: str) -> pd.DataFrame:
    """Fetch free financial indicators through AKShare when available."""
    import akshare as ak

    symbol = ts_code.split(".")[0]
    df = ak.stock_financial_analysis_indicator(symbol=symbol)
    if df.empty:
        return df
    df = df.copy()
    df["ts_code"] = ts_code
    df["source"] = "akshare.stock_financial_analysis_indicator"
    df["updated_at"] = pd.Timestamp.now()
    return df


def fetch_quarterly_finance_baostock(ts_code: str, year: int, quarter: int) -> pd.DataFrame:
    """Fetch BaoStock quarterly profit data, no token required."""
    import baostock as bs

    code, exchange = ts_code.split(".")
    bs_code = f"{exchange.lower()}.{code}"
    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query_profit_data failed: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        if not df.empty:
            df["ts_code"] = ts_code
            df["source"] = "baostock.query_profit_data"
            df["updated_at"] = pd.Timestamp.now()
        return df
    finally:
        bs.logout()
