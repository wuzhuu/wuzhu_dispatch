from __future__ import annotations

from datetime import datetime

import pandas as pd


def _normalize_akshare_daily(df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rename_map = {
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "vol",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "change",
        "换手率": "turnover_rate",
    }
    out = df.rename(columns=rename_map).copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y%m%d")
    out["ts_code"] = ts_code
    out["pre_close"] = None
    out["source"] = "akshare.stock_zh_a_hist"
    out["updated_at"] = pd.Timestamp.now()
    return out[["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount", "source", "updated_at"]]


def fetch_daily_price(ts_code: str, trade_date: str, adjust: str = "") -> pd.DataFrame:
    """Fetch one stock daily bar from AKShare stock_zh_a_hist, no token required."""
    import akshare as ak

    symbol = ts_code.split(".")[0]
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=trade_date, end_date=trade_date, adjust=adjust)
    return _normalize_akshare_daily(df, ts_code)


def fetch_daily_price_batch(ts_codes: list[str], trade_date: str, max_stocks: int = 0) -> pd.DataFrame:
    selected = ts_codes[:max_stocks] if max_stocks and max_stocks > 0 else ts_codes
    frames: list[pd.DataFrame] = []
    for ts_code in selected:
        try:
            df = fetch_daily_price(ts_code, trade_date)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_daily_price_baostock(ts_code: str, trade_date: str, adjustflag: str = "3") -> pd.DataFrame:
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        return _query_daily_price_baostock(bs, ts_code, trade_date, adjustflag)
    finally:
        bs.logout()


def _query_daily_price_baostock(bs, ts_code: str, trade_date: str, adjustflag: str = "3") -> pd.DataFrame:
    code, exchange = ts_code.split(".")
    bs_code = f"{exchange.lower()}.{code}"
    day = datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,preclose,volume,amount,pctChg",
        start_date=day,
        end_date=day,
        frequency="d",
        adjustflag=adjustflag,
    )
    if rs.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    raw = pd.DataFrame(rows, columns=rs.fields)
    if raw.empty:
        return raw
    raw = raw.rename(columns={"date": "trade_date", "preclose": "pre_close", "volume": "vol", "pctChg": "pct_chg"})
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.strftime("%Y%m%d")
    raw["ts_code"] = ts_code
    raw["change"] = pd.to_numeric(raw["close"], errors="coerce") - pd.to_numeric(raw["pre_close"], errors="coerce")
    raw["source"] = "baostock.query_history_k_data_plus"
    raw["updated_at"] = pd.Timestamp.now()
    return raw[["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount", "source", "updated_at"]]


def fetch_adjusted_daily_price(ts_code: str, trade_date: str, adjust: str = "qfq") -> pd.DataFrame:
    """Fetch front/back adjusted daily bar through AKShare. adjust: qfq or hfq."""
    df = fetch_daily_price(ts_code, trade_date, adjust=adjust)
    if not df.empty:
        df["adjust"] = adjust
        df["source"] = f"akshare.stock_zh_a_hist.{adjust}"
    return df


def fetch_daily_price_batch_baostock_first(ts_codes: list[str], trade_date: str, max_stocks: int = 0) -> pd.DataFrame:
    """批量获取日线：非北交所走 baostock→akshare，北交所走 tencent→akshare。"""
    import baostock as bs

    selected = ts_codes[:max_stocks] if max_stocks and max_stocks > 0 else ts_codes
    frames: list[pd.DataFrame] = []
    login_result = bs.login()
    baostock_ready = login_result.error_code == "0"
    try:
        for ts_code in selected:
            try:
                if ts_code.upper().endswith(".BJ"):
                    # 北交所：优先腾讯 API，fallback akshare
                    df = _fetch_daily_price_tencent(ts_code, trade_date)
                    if df.empty:
                        df = fetch_daily_price(ts_code, trade_date)
                elif baostock_ready:
                    df = _query_daily_price_baostock(bs, ts_code, trade_date)
                    if df.empty:
                        df = fetch_daily_price(ts_code, trade_date)
                else:
                    df = fetch_daily_price(ts_code, trade_date)
                if not df.empty:
                    frames.append(df)
            except Exception:
                try:
                    df = fetch_daily_price(ts_code, trade_date)
                    if not df.empty:
                        frames.append(df)
                except Exception:
                    continue
    finally:
        if baostock_ready:
            bs.logout()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fetch_daily_price_tencent(ts_code: str, trade_date: str) -> pd.DataFrame:
    """通过 AKShare 腾讯源获取单日日线（ak.stock_zh_a_hist_tx）。"""
    from src.collectors.free_market_sources import fetch_tencent_hist

    try:
        df = fetch_tencent_hist(ts_code, trade_date, trade_date, adjust="qfq")
        if df.empty:
            return df
        df = _normalize_akshare_daily(df, ts_code)
        df["source"] = "akshare.stock_zh_a_hist_tx"
        return df
    except Exception:
        return pd.DataFrame()
