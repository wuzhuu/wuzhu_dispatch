from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import requests


SINA_HISTORY_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{symbol}_{scale}_{end_date}/CN_MarketDataService.getKLineData"
TENCENT_HISTORY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


@dataclass(frozen=True)
class BseDailyConfig:
    timeout: float = 12.0
    retries: int = 2
    request_interval: float = 0.25


def fetch_bse_daily(ts_code: str, start_date: str, end_date: str, config: BseDailyConfig | None = None) -> pd.DataFrame:
    """Fetch BSE OHLCV data without routing through the legacy market provider stack."""
    config = config or BseDailyConfig()
    errors: list[str] = []
    for source_name, fetcher in (
        ("sina_history_direct", _fetch_sina_history_direct),
        ("tencent_history_direct", _fetch_tencent_history_direct),
    ):
        try:
            df = fetcher(ts_code, start_date, end_date, config)
            if not df.empty:
                return _normalize_ohlcv(df, ts_code, source_name, start_date, end_date)
            errors.append(f"{source_name} returned empty dataframe")
        except Exception as exc:
            errors.append(f"{source_name} failed: {exc!r}")
    empty = pd.DataFrame(columns=_columns())
    empty.attrs["error"] = "; ".join(errors)
    return empty


def _fetch_sina_history_direct(ts_code: str, start_date: str, end_date: str, config: BseDailyConfig) -> pd.DataFrame:
    symbol = _sina_symbol(ts_code)
    end = _to_yyyymmdd(end_date)
    url = SINA_HISTORY_URL.format(symbol=symbol, scale="240", end_date=end)
    text = _request_text(url, config)
    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if start_idx < 0 or end_idx < start_idx:
        return pd.DataFrame()
    rows = json.loads(text[start_idx : end_idx + 1])
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).rename(columns={"day": "trade_date"})


def _fetch_tencent_history_direct(ts_code: str, start_date: str, end_date: str, config: BseDailyConfig) -> pd.DataFrame:
    symbol = _tencent_symbol(ts_code)
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    start_year = max(int(start[:4]), 2021)
    end_year = int(end[:4])
    frames = []
    for year in range(start_year, end_year + 1, 2):
        time.sleep(max(0.0, config.request_interval))
        chunk_end = min(year + 2, end_year + 1)
        payload = _request_json(
            TENCENT_HISTORY_URL,
            {
                "_var": f"kline_day{year}",
                "param": f"{symbol},day,{year}-01-01,{chunk_end}-01-01,640,qfq",
                "r": str(time.time()),
            },
            config,
        )
        stock_data = payload.get("data", {}).get(symbol, {})
        rows = stock_data.get("qfqday") or stock_data.get("day") or []
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        if frame.empty:
            continue
        frame = frame.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8]].copy()
        frame.columns = ["trade_date", "open", "close", "high", "low", "volume", "pct_chg", "amount"]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _normalize_ohlcv(df: pd.DataFrame, ts_code: str, source: str, start_date: str, end_date: str) -> pd.DataFrame:
    out = df.copy()
    rename_map = {
        "date": "trade_date",
        "day": "trade_date",
        "vol": "volume",
        "turnover": "amount",
    }
    out = out.rename(columns={col: rename_map[col] for col in out.columns if col in rename_map})
    for col in _columns():
        if col not in out.columns:
            out[col] = pd.NA
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    start_iso = _to_iso_date(start_date)
    end_iso = _to_iso_date(end_date)
    out = out[(out["trade_date"] >= start_iso) & (out["trade_date"] <= end_iso)].copy()
    out["ts_code"] = _normalize_ts_code(ts_code)
    out["source"] = source
    out["updated_at"] = pd.Timestamp.now()
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["trade_date", "open", "high", "low", "close"])
    out = out.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return out[_columns()].reset_index(drop=True)


def _request_text(url: str, config: BseDailyConfig) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, config.retries + 1)):
        try:
            response = requests.get(url, timeout=config.timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4)
    raise RuntimeError(f"request failed: {last_exc!r}")


def _request_json(url: str, params: dict[str, object], config: BseDailyConfig) -> dict:
    text = _request_text_with_params(url, params, config)
    json_text = text[text.find("={") + 1 :] if "={" in text else text
    return json.loads(json_text)


def _request_text_with_params(url: str, params: dict[str, object], config: BseDailyConfig) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, config.retries + 1)):
        try:
            response = requests.get(url, params=params, timeout=config.timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4)
    raise RuntimeError(f"request failed: {last_exc!r}")


def _columns() -> list[str]:
    return ["trade_date", "ts_code", "open", "high", "low", "close", "volume", "amount", "pct_chg", "source", "updated_at"]


def _normalize_ts_code(ts_code: object) -> str:
    text = str(ts_code).strip()
    if "." in text:
        left, right = text.split(".", 1)
        if len(left) <= 3 and left.isalpha():
            return f"{left.lower()}.{right.zfill(6)}"
        return f"{right.lower()}.{left.zfill(6)}"
    return f"bj.{text.zfill(6)}"


def _symbol_only(ts_code: object) -> str:
    return _normalize_ts_code(ts_code).split(".", 1)[1]


def _sina_symbol(ts_code: object) -> str:
    return "bj" + _symbol_only(ts_code)


def _tencent_symbol(ts_code: object) -> str:
    return "bj" + _symbol_only(ts_code)


def _to_yyyymmdd(date_value: str) -> str:
    text = str(date_value)
    if "-" in text:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y%m%d")
    return text


def _to_iso_date(date_value: str) -> str:
    text = str(date_value)
    if "-" in text:
        return text
    return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
