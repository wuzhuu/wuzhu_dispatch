from __future__ import annotations

import json
import time
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

from akshare.utils.tqdm import get_tqdm
from src.utils.retry import retry_api


# ---------------------------------------------------------------------------
# AKShare 腾讯源日线（ak.stock_zh_a_hist_tx），北交所优先尝试
# ---------------------------------------------------------------------------
_tencent_last_request_at = 0.0
_TENCENT_MIN_INTERVAL = 1.1


def _sleep_before_tencent() -> None:
    """腾讯 API 限速：确保两次请求间隔 ≥ _TENCENT_MIN_INTERVAL 秒。"""
    global _tencent_last_request_at
    elapsed = time.monotonic() - _tencent_last_request_at
    if elapsed < _TENCENT_MIN_INTERVAL:
        time.sleep(_TENCENT_MIN_INTERVAL - elapsed)
    _tencent_last_request_at = time.monotonic()


def _to_tencent_symbol(ts_code: str) -> str:
    """将 ts_code 转为腾讯格式（bj920000）。

    支持两种输入格式：
    - baostock 格式: bj.920000 / sh.600000 / sz.000001
    - 后缀格式:    920000.BJ / 600000.SH / 000001.SZ
    """
    parts = ts_code.split(".")
    if len(parts) != 2:
        return ts_code
    left, right = parts
    # baostock 格式: exchange 在前（短如 bj/sh/sz）
    if len(left) <= 3 and left.isalpha():
        return f"{left.lower()}{right}"
    # 后缀格式: exchange 在后（如 .BJ / .SH / .SZ）
    return f"{right.lower()}{left}"


def _to_yyyymmdd(date_str: str) -> str:
    """将 YYYYMMDD 或 YYYY-MM-DD 统一转为 YYYYMMDD。"""
    date_str = str(date_str)
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
    return date_str


def _to_baostock_code(ts_code: str) -> str:
    code, exchange = ts_code.split(".")
    return f"{exchange.lower()}.{code}"


def _to_yahoo_symbol(ts_code: str) -> str:
    code, exchange = ts_code.split(".")
    suffix = "SS" if exchange.upper() == "SH" else "SZ"
    return f"{code}.{suffix}"


def _to_stooq_symbol(ts_code: str) -> str:
    code, exchange = ts_code.split(".")
    suffix = "cn" if exchange.upper() in {"SH", "SZ"} else exchange.lower()
    return f"{code}.{suffix}"


def fetch_akshare_hist(ts_code: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
    import akshare as ak

    symbol = ts_code.split(".")[0]
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)
    df["ts_code"] = ts_code
    df["source"] = "akshare"
    df["updated_at"] = pd.Timestamp.now()
    return df


def fetch_baostock_hist(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            _to_baostock_code(ts_code),
            "date,code,open,high,low,close,preclose,volume,amount,pctChg",
            start_date=datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d"),
            end_date=datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        df["ts_code"] = ts_code
        df["source"] = "baostock"
        df["updated_at"] = pd.Timestamp.now()
        return df
    finally:
        bs.logout()


def fetch_yahoo_hist(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import yfinance as yf

    symbol = _to_yahoo_symbol(ts_code)
    df = yf.download(
        symbol,
        start=datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d"),
        end=datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        return df
    df = df.reset_index()
    df["ts_code"] = ts_code
    df["source"] = "yahoo_finance"
    df["updated_at"] = pd.Timestamp.now()
    return df


@retry_api()
def fetch_tencent_hist(
    ts_code: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """通过腾讯公开 API 获取日线历史数据（北交所与沪深通用）。

    直接调用腾讯 proxy.finance.qq.com 接口，绕过 AKShare 的 stock_zh_a_hist_tx，
    因为该函数内部的 get_tx_start_year() 对北交所 bj. 前缀存在 KeyError 崩溃。

    腾讯 API 单次最多返回 640 行，因此将请求按 2 年跨度分块（2021-2022, 2023-2024, 2025-2027），
    去重后合并返回。

    参数
    ----
    ts_code : str
        股票代码，如 ``bj.920000`` / ``sh.600000`` / ``sz.000001``。
    start_date / end_date : str
        YYYYMMDD 或 YYYY-MM-DD 格式。
    adjust : str
        ``qfq`` 前复权, ``hfq`` 后复权, ``""`` 不复权。
        *注意：腾讯 API 复权通过不同的 key 返回（day/qfqday/hfqday），
        暂不支持 hfq。*

    返回
    ----
    pd.DataFrame
        列名格式与 stock_zh_a_hist_tx 兼容：
        ``date``, ``open``, ``close``, ``high``, ``low``, ``volume``, ``amount``
    """
    _sleep_before_tencent()

    # 统一格式
    symbol = _to_tencent_symbol(ts_code)  # bj.920000 → bj920000
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    start_year = int(start[:4])
    end_year = int(end[:4])

    # 判断 API key 名称
    if not adjust:
        data_key = "day"
    elif adjust == "qfq":
        data_key = "qfqday"
    elif adjust == "hfq":
        data_key = "hfqday"
    else:
        data_key = "day"

    # 分块：每块最多包含 2 个完整年份，确保单次不超 640 行
    year_chunks = []
    y = max(start_year, 2021)
    while y <= end_year:
        chunk_end = min(y + 2, end_year + 1)
        year_chunks.append((y, chunk_end))
        y = chunk_end

    # 腾讯 API 参数
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"

    tqdm = get_tqdm()
    big_df = pd.DataFrame()

    for chunk_start, chunk_end in tqdm(year_chunks, leave=False):
        _sleep_before_tencent()
        params = {
            "_var": f"kline_day{chunk_start}",
            "param": f"{symbol},day,{chunk_start}-01-01,{chunk_end}-01-01,640,{adjust}",
            "r": str(time.time()),
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        raw_text = r.text
        try:
            json_str = raw_text[raw_text.find("={") + 1:]
            data = json.loads(json_str)
            stock_data = data.get("data", {}).get(symbol, {})
            rows = stock_data.get(data_key) or stock_data.get("day") or stock_data.get("qfqday") or []
        except (AttributeError, KeyError, json.JSONDecodeError, TypeError, ValueError):
            # API 返回异常格式（如 list 替代 dict）时静默跳过
            rows = []

        if not rows:
            continue

        temp_df = pd.DataFrame(rows)
        if temp_df.empty:
            continue

        # 腾讯 API 原始列格式：
        #   0: date, 1: open, 2: close, 3: high, 4: low,
        #   5: volume (手), 6: {}, 7: pct_chg (%), 8: amount (万元)
        temp_df = temp_df.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8]].copy()
        temp_df.columns = ["date", "open", "close", "high", "low", "volume", "pct_chg", "amount"]

        # volume: 手 → 股
        temp_df["volume"] = pd.to_numeric(temp_df["volume"], errors="coerce").fillna(0).astype("float64")
        # amount: 万元 → 元
        temp_df["amount"] = pd.to_numeric(temp_df["amount"], errors="coerce").fillna(0).astype("float64")
        # pct_chg
        temp_df["pct_chg"] = pd.to_numeric(temp_df["pct_chg"], errors="coerce").fillna(0.0)

        big_df = pd.concat([big_df, temp_df], ignore_index=True)

    if big_df.empty:
        return big_df

    # 去重
    big_df = big_df.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    big_df = big_df.sort_values("date").reset_index(drop=True)

    # 过滤日期范围（统一转为 YYYYMMDD 字符串比较）
    start_iso = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_iso = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    big_df = big_df[(big_df["date"] >= start_iso) & (big_df["date"] <= end_iso)].copy()

    # 数值化
    for col in ("open", "close", "high", "low"):
        big_df[col] = pd.to_numeric(big_df[col], errors="coerce")

    return big_df


@retry_api()
def fetch_stooq_csv(ts_code: str) -> pd.DataFrame:
    symbol = _to_stooq_symbol(ts_code)
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))
    df["ts_code"] = ts_code
    df["source"] = "stooq"
    df["updated_at"] = pd.Timestamp.now()
    return df
