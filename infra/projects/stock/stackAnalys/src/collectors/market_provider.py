from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Callable

import pandas as pd
import requests
from loguru import logger

from src.collectors.stock_basic import fetch_stock_basic_baostock
from src.providers.bse_daily import fetch_bse_daily
from src.utils.config import load_settings
from src.utils.proxy import proxy_disabled


# baostock TCP socket 不支持并发访问，全局锁序列化所有请求
_BAOSTOCK_LOCK = threading.Lock()


def _set_baostock_socket_timeout(timeout: float) -> None:
    """设置 baostock 底层 socket 超时，防止网络问题导致永久阻塞。"""
    try:
        import baostock.common.context as _bs_ctx

        if hasattr(_bs_ctx, "default_socket") and _bs_ctx.default_socket is not None:
            _bs_ctx.default_socket.settimeout(timeout)
    except Exception:
        logger.warning("Failed to set baostock socket timeout")


REQUEST_EXCEPTIONS = (requests.RequestException, ConnectionError, TimeoutError)


@dataclass
class ProviderRunInfo:
    data_name: str
    source: str
    fallback_used: bool
    primary_error: str = ""
    fallback_error: str = ""
    success: bool = False


class MarketDataProvider:
    def __init__(self) -> None:
        settings = load_settings()
        self.settings = settings
        self.adjust_type = settings.get("market", {}).get("adjust_type", "qfq")
        self.baostock_adjustflag = str(settings.get("market", {}).get("baostock_adjustflag", "2"))
        self.retry_times = int(settings.get("collector", {}).get("retry_times", 3))
        self.retry_sleep_seconds = int(settings.get("collector", {}).get("retry_sleep_seconds", 5))
        self.fallback_circuit_breaker_failures = int(settings.get("collector", {}).get("fallback_circuit_breaker_failures", 10))
        self.fallback_circuit_breaker_cooldown_seconds = int(settings.get("collector", {}).get("fallback_circuit_breaker_cooldown_seconds", 3600))
        self.request_sleep_min = float(settings.get("collector", {}).get("request_sleep_min", 0.5))
        self.request_sleep_max = float(settings.get("collector", {}).get("request_sleep_max", 1.5))
        self.akshare_request_min_interval = float(settings.get("collector", {}).get("akshare_request_min_interval", 1.1))
        self.tencent_request_min_interval = float(settings.get("collector", {}).get("tencent_request_min_interval", 1.1))
        self.source_cfg = settings.get("data_source", {})
        self.daily_price_primary = self._configured_source("daily_price_history_primary", self.source_cfg.get("daily_price_primary", "baostock"))
        self.daily_price_fallback = self._configured_source("daily_price_history_fallback", self.source_cfg.get("daily_price_fallback", "eastmoney"))
        self.daily_price_bse_primary = self._configured_source("daily_price_bse_primary", "eastmoney")
        self.daily_price_bse_fallback = self._configured_source("daily_price_bse_fallback", "baostock")
        self.index_daily_primary = self._configured_source("index_daily_primary", "baostock")
        self.index_daily_fallback = self._configured_source("index_daily_fallback", "eastmoney")
        self.stock_basic_primary = self._configured_source("stock_basic_primary", "akshare")
        self.stock_basic_fallback = self._configured_source("stock_basic_fallback", "baostock")
        self._last_akshare_request_at = 0.0
        self._last_tencent_request_at = 0.0
        self._baostock: ModuleType | None = None
        self._baostock_logged_in = False
        self._source_failure_counts: dict[str, int] = {}
        self._source_disabled_until: dict[str, float] = {}
        self.last_run: ProviderRunInfo | None = None

    def get_stock_basic(self) -> pd.DataFrame:
        return self._with_fallback(
            data_name="stock_basic",
            primary_func=lambda: self._stock_basic_by_source(self.stock_basic_primary),
            fallback_func=lambda: self._stock_basic_by_source(self.stock_basic_fallback),
            primary_source=self.stock_basic_primary,
            fallback_source=self.stock_basic_fallback,
        )

    def get_daily_price(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = self._normalize_baostock_code(ts_code)
        if self._is_bse_code(code):
            df = fetch_bse_daily(code, start_date, end_date)
            source = str(df["source"].iloc[0]) if not df.empty and "source" in df.columns else ""
            self.last_run = ProviderRunInfo(
                data_name=f"daily_price:{code}",
                source=source,
                fallback_used=source == "tencent_history_direct",
                primary_error=str(df.attrs.get("error", "")),
                success=not df.empty,
            )
            if df.empty:
                logger.warning("daily_price:{} BSE provider returned empty: {}", code, df.attrs.get("error", ""))
            return self._normalize_daily_price(df, code, source or "bse_daily_direct")
        primary = self.daily_price_primary
        fallback = self.daily_price_fallback
        return self._with_fallback(
            data_name=f"daily_price:{code}",
            primary_func=lambda: self._daily_price_by_source(primary, code, start_date, end_date),
            fallback_func=lambda: self._daily_price_by_source(fallback, code, start_date, end_date),
            primary_source=primary,
            fallback_source=fallback,
        )

    def get_daily_price_multi_source(
        self, ts_code: str, start_date: str, end_date: str, sources: list[str]
    ) -> pd.DataFrame:
        """用指定源列表依次尝试获取日线数据，跳过熔断的源。"""
        code = self._normalize_baostock_code(ts_code)
        if self._is_bse_code(code):
            df = fetch_bse_daily(code, start_date, end_date)
            source = str(df["source"].iloc[0]) if not df.empty and "source" in df.columns else ""
            if df.empty:
                logger.warning("daily_price:{} BSE provider returned empty for multi_source: {}", code, df.attrs.get("error", ""))
            return self._normalize_daily_price(df, code, source or "bse_daily_direct")
        errors = []
        for source in sources:
            normalized = self._normalize_source_name(source)
            if self._source_is_disabled(normalized):
                errors.append(f"{source} disabled by circuit breaker")
                continue
            try:
                df = self._daily_price_by_source(source, code, start_date, end_date)
                if not df.empty:
                    self._record_source_success(normalized)
                    return df
            except Exception as exc:
                self._record_source_failure(normalized, exc)
                errors.append(f"{source}: {exc!r}")
        logger.warning("get_daily_price_multi_source all {} failed for {}: {}", sources, ts_code, "; ".join(errors))
        return pd.DataFrame()

    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = self._normalize_baostock_code(index_code)
        return self._with_fallback(
            data_name=f"index_daily:{code}",
            primary_func=lambda: self._index_daily_by_source(self.index_daily_primary, code, start_date, end_date),
            fallback_func=lambda: self._index_daily_by_source(self.index_daily_fallback, code, start_date, end_date),
            primary_source=self.index_daily_primary,
            fallback_source=self.index_daily_fallback,
        )

    def source_config_summary(self) -> str:
        return (
            f"daily_price={self.daily_price_primary}->{self.daily_price_fallback}; "
            f"daily_price_bse={self.daily_price_bse_primary}->{self.daily_price_bse_fallback}; "
            f"index_daily={self.index_daily_primary}->{self.index_daily_fallback}; "
            f"stock_basic={self.stock_basic_primary}->{self.stock_basic_fallback}"
        )

    def _stock_basic_akshare(self) -> pd.DataFrame:
        import akshare as ak

        with proxy_disabled():
            df = ak.stock_info_a_code_name()
        if df.empty:
            return df
        out = df.copy()
        code_col = "code" if "code" in out.columns else "代码"
        name_col = "name" if "name" in out.columns else "名称"
        out["symbol"] = out[code_col].astype(str).str.zfill(6)
        out["ts_code"] = out["symbol"].map(self._symbol_to_baostock_code)
        out["name"] = out[name_col]
        out["area"] = None
        out["industry"] = None
        out["market"] = None
        out["list_date"] = pd.NaT
        out["exchange"] = out["ts_code"].str.split(".").str[0].str.upper().map({"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"})
        out["is_hs"] = None
        out["updated_at"] = pd.Timestamp.now()
        out["source"] = "akshare.stock_info_a_code_name"
        return out[["ts_code", "symbol", "name", "area", "industry", "market", "list_date", "exchange", "is_hs", "updated_at", "source"]]

    def _stock_basic_by_source(self, source: str) -> pd.DataFrame:
        normalized = self._normalize_source_name(source)
        if normalized in {"akshare", "eastmoney"}:
            return self._stock_basic_akshare()
        if normalized == "baostock":
            with _BAOSTOCK_LOCK:
                _set_baostock_socket_timeout(60.0)
                df = fetch_stock_basic_baostock()
                if not df.empty:
                    df["source"] = "baostock.query_all_stock"
            return df
        raise ValueError(f"Unsupported stock_basic source: {source}")

    def _daily_price_by_source(self, source: str, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        normalized = self._normalize_source_name(source)
        if normalized == "baostock":
            if self._is_bse_code(ts_code):
                raise ValueError("BaoStock does not support BSE daily_price")
            return self._daily_price_baostock(ts_code, start_date, end_date)
        if normalized == "tencent":
            return self._daily_price_tencent(ts_code, start_date, end_date)
        if normalized == "akshare_tx_hist":
            return self._daily_price_tencent(ts_code, start_date, end_date)
        if normalized in {"akshare", "eastmoney"}:
            return self._daily_price_akshare(ts_code, start_date, end_date)
        if normalized == "westock":
            return self._daily_price_westock(ts_code, start_date, end_date)
        raise ValueError(f"Unsupported daily_price source: {source}")

    def _index_daily_by_source(self, source: str, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        normalized = self._normalize_source_name(source)
        if normalized == "baostock":
            return self._index_daily_baostock(index_code, start_date, end_date)
        if normalized in {"akshare", "eastmoney"}:
            return self._index_daily_akshare(index_code, start_date, end_date)
        if normalized == "sina":
            return self._index_daily_sina(index_code, start_date, end_date)
        raise ValueError(f"Unsupported index_daily source: {source}")

    def _with_fallback(
        self,
        data_name: str,
        primary_func,
        fallback_func,
        primary_error_override: str = "",
        primary_source: str = "",
        fallback_source: str = "",
    ) -> pd.DataFrame:
        primary_error = primary_error_override
        if not primary_error_override:
            try:
                df = primary_func()
                if not df.empty:
                    source = str(df["source"].iloc[0])
                    self._record_source_success(primary_source or source)
                    self.last_run = ProviderRunInfo(data_name, source, False, success=True)
                    logger.info("{} primary succeeded source={} rows={}", data_name, source, len(df))
                    return df
                primary_error = "primary returned empty dataframe"
                logger.warning("{} primary failed: {}", data_name, primary_error)
            except Exception as exc:
                primary_error = repr(exc)
                logger.exception("{} primary failed: {}", data_name, primary_error)

        if fallback_source and self._source_is_disabled(fallback_source):
            fallback_error = f"fallback source {fallback_source} disabled by circuit breaker"
            self.last_run = ProviderRunInfo(data_name, "", True, primary_error, fallback_error, False)
            logger.error("{} fallback skipped: {}; primary_error={}", data_name, fallback_error, primary_error)
            return pd.DataFrame()

        try:
            df = fallback_func()
            if not df.empty:
                source = str(df["source"].iloc[0])
                self._record_source_success(fallback_source or source)
                self.last_run = ProviderRunInfo(data_name, source, True, primary_error=primary_error, success=True)
                logger.warning("{} fallback succeeded source={} primary_error={}", data_name, source, primary_error)
                return df
            fallback_error = "fallback returned empty dataframe"
            self.last_run = ProviderRunInfo(data_name, "", True, primary_error, fallback_error, False)
            logger.error("{} fallback failed: {}; primary_error={}", data_name, fallback_error, primary_error)
            return df
        except Exception as exc:
            fallback_error = repr(exc)
            self._record_source_failure(fallback_source, exc)
            self.last_run = ProviderRunInfo(data_name, "", True, primary_error, fallback_error, False)
            logger.exception("{} fallback failed: {}; primary_error={}", data_name, fallback_error, primary_error)
            raise

    @contextmanager
    def baostock_session(self):
        self._ensure_baostock_login()
        try:
            yield
        finally:
            self.close_baostock_session()

    def _ensure_baostock_login(self) -> ModuleType:
        with _BAOSTOCK_LOCK:
            if self._baostock is None:
                import baostock as bs

                self._baostock = bs
            if not self._baostock_logged_in:
                # 先设超时，再 login：防止 login 本身挂起
                _set_baostock_socket_timeout(60.0)
                login_result = self._baostock.login()
                if login_result.error_code != "0":
                    raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
                self._baostock_logged_in = True
        return self._baostock

    def close_baostock_session(self) -> None:
        with _BAOSTOCK_LOCK:
            if self._baostock is not None and self._baostock_logged_in:
                _set_baostock_socket_timeout(60.0)
                self._baostock.logout()
                self._baostock_logged_in = False

    def _daily_price_baostock(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        import socket as _socket
        # 先登录（_ensure_baostock_login 自带锁），再取查询锁
        bs = self._ensure_baostock_login()
        with _BAOSTOCK_LOCK:
            orig_timeout = _socket.getdefaulttimeout()
            _socket.setdefaulttimeout(60)
            try:
                rs = bs.query_history_k_data_plus(
                ts_code,
                "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn,tradestatus,isST",
                start_date=self._to_iso_date(start_date),
                end_date=self._to_iso_date(end_date),
                frequency="d",
                adjustflag=self.baostock_adjustflag,
            )
            finally:
                _socket.setdefaulttimeout(orig_timeout)
            if rs.error_code != "0":
                raise RuntimeError(f"BaoStock daily query failed: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            raw = pd.DataFrame(rows, columns=rs.fields)
            return self._normalize_daily_price(raw, ts_code, "baostock.query_history_k_data_plus")

    def _index_daily_baostock(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        bs = self._ensure_baostock_login()
        with _BAOSTOCK_LOCK:
            rs = bs.query_history_k_data_plus(
                index_code,
                "date,code,open,high,low,close,preclose,volume,amount,pctChg",
                start_date=self._to_iso_date(start_date),
                end_date=self._to_iso_date(end_date),
                frequency="d",
                adjustflag="3",
            )
            if rs.error_code != "0":
                raise RuntimeError(f"BaoStock index query failed: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            raw = pd.DataFrame(rows, columns=rs.fields)
            return self._normalize_index_daily(raw, index_code, "baostock.query_history_k_data_plus.index")

    def _daily_price_tencent(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 AKShare 腾讯源获取日线数据（ak.stock_zh_a_hist_tx，北交所优先）。"""
        return self._run_request_with_retries(
            "daily_price_tencent",
            lambda: self._daily_price_tencent_once(ts_code, start_date, end_date),
        )

    def _daily_price_tencent_once(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        from src.collectors.free_market_sources import fetch_tencent_hist

        df = fetch_tencent_hist(ts_code, start_date, end_date, adjust=self.adjust_type)
        return self._normalize_daily_price(df, ts_code, f"akshare.stock_zh_a_hist_tx.{self.adjust_type}")

    def _daily_price_akshare(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._run_request_with_retries(
            "daily_price_akshare",
            lambda: self._daily_price_akshare_once(ts_code, start_date, end_date),
        )

    def _daily_price_akshare_once(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self._sleep_before_akshare()
        import akshare as ak

        symbol = ts_code.split(".")[1]
        with proxy_disabled():
            raw = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=self._to_yyyymmdd(start_date),
                end_date=self._to_yyyymmdd(end_date),
                adjust=self.adjust_type,
            )
        return self._normalize_daily_price(raw, ts_code, f"akshare.stock_zh_a_hist.{self.adjust_type}")

    @staticmethod
    def _normalize_westock_code(ts_code: str) -> str:
        """将 ts_code (sh.600519) 转为 westock 格式 (sh600519)"""
        return ts_code.replace(".", "")

    def _daily_price_westock(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 westock-data CLI 获取日线数据（腾讯自选股数据源）。"""
        import subprocess

        westock_code = self._normalize_westock_code(ts_code)
        limit = 10  # 足够覆盖短期查询

        cmd = ["npx", "-y", "westock-data-skillhub@1.0.3", "kline", westock_code,
               "--period", "day", "--limit", str(limit)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning("westock CLI failed for {}: {}", ts_code, result.stderr[:200])
                return pd.DataFrame()
        except subprocess.TimeoutExpired:
            logger.warning("westock CLI timeout for {}", ts_code)
            return pd.DataFrame()

        output = result.stdout.strip()
        # 统一解析 Markdown 表格：找以 | 开头的行（跳过 batch 状态行）
        lines = output.split("\n")
        table_lines = [l for l in lines if l.startswith("|")]
        if len(table_lines) < 3:  # 至少 header + separator + 1 data row
            return pd.DataFrame()

        # 检测表头列数来判断是 batch（有 symbol 列）还是 single
        header_parts = [p.strip() for p in table_lines[0].split("|")[1:-1]]
        has_symbol_col = "symbol" in [p.lower() for p in header_parts]

        data_rows = []
        for line in table_lines[2:]:  # 跳过 header 和 separator
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) < 7:
                continue
            try:
                if has_symbol_col:
                    sym, date_str, open_p, last_p, high_p, low_p, vol, amt = parts[:8]
                    if sym != westock_code:
                        continue
                else:
                    date_str, open_p, last_p, high_p, low_p, vol, amt = parts[:7]
                data_rows.append({
                    "date": date_str,
                    "open": float(open_p),
                    "high": float(high_p),
                    "low": float(low_p),
                    "close": float(last_p),
                    "volume": int(float(vol)),
                    "amount": float(amt),
                })
            except (ValueError, IndexError):
                continue

        if not data_rows:
            return pd.DataFrame()

        df = pd.DataFrame(data_rows)
        df = df.sort_values("date")
        # 计算 preclose: 前一行的 close 作为当前行的 preclose
        df["preclose"] = df["close"].shift(1)
        # 过滤日期范围
        df = df[(df["date"] >= start_date[:10]) & (df["date"] <= end_date[:10])]
        if df.empty:
            return pd.DataFrame()

        return self._normalize_daily_price(df, ts_code, "westock-data.kline")

    def _index_daily_akshare(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._run_request_with_retries(
            "index_daily_akshare",
            lambda: self._index_daily_akshare_once(index_code, start_date, end_date),
        )

    def _index_daily_akshare_once(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self._sleep_before_akshare()
        import akshare as ak

        symbol = index_code.split(".")[1]
        with proxy_disabled():
            raw = ak.index_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=self._to_yyyymmdd(start_date),
                end_date=self._to_yyyymmdd(end_date),
            )
        return self._normalize_index_daily(raw, index_code, "akshare.index_zh_a_hist")

    def _index_daily_sina(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 AKShare 新浪源获取指数日线数据"""
        return self._run_request_with_retries(
            "index_daily_sina",
            lambda: self._index_daily_sina_once(index_code, start_date, end_date),
        )

    def _index_daily_sina_once(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self._sleep_before_akshare()
        import akshare as ak

        # baostock 格式如 sh.000001 → 新浪格式 sh000001（去掉点号）
        symbol = index_code.replace(".", "")
        with proxy_disabled():
            raw = ak.stock_zh_index_daily(symbol=symbol)
        if raw.empty:
            return raw
        # 按日期范围过滤（date 列是 datetime.date，转为字符串比较）
        raw["_date_str"] = raw["date"].astype(str)
        mask = (raw["_date_str"] >= start_date) & (raw["_date_str"] <= end_date)
        raw = raw[mask].drop(columns=["_date_str"]).copy()
        raw["source"] = "sina.stock_zh_index_daily"
        return self._normalize_index_daily(raw, index_code, "sina.stock_zh_index_daily")

    def _run_request_with_retries(self, label: str, fetcher: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        attempts = max(1, self.retry_times)
        for attempt in range(1, attempts + 1):
            try:
                return fetcher()
            except REQUEST_EXCEPTIONS:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "{} request failed attempt={}/{}; retry in {}s",
                    label,
                    attempt,
                    attempts,
                    self.retry_sleep_seconds,
                )
                time.sleep(max(0, self.retry_sleep_seconds))
        return pd.DataFrame()

    def _normalize_daily_price(self, raw: pd.DataFrame, ts_code: str, source: str) -> pd.DataFrame:
        cols = ["ts_code", "trade_date", "open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "turn", "tradestatus", "is_st", "adjust_type", "source", "updated_at"]
        if raw.empty:
            return pd.DataFrame(columns=cols)
        df = raw.rename(columns={
            "date": "trade_date", "日期": "trade_date", "code": "ts_code", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "preclose": "preclose", "成交量": "volume", "成交额": "amount", "pctChg": "pct_chg", "涨跌幅": "pct_chg", "换手率": "turn", "turn": "turn", "isST": "is_st",
        }).copy()
        df["ts_code"] = ts_code
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        for col in ("open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "turn"):
            df[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else None
        for col in ("tradestatus", "is_st"):
            df[col] = df[col] if col in df.columns else None
        df["adjust_type"] = self.adjust_type if self.baostock_adjustflag == "2" or "akshare" in source else None
        df["source"] = source
        df["updated_at"] = pd.Timestamp.now()
        return df[cols]

    def _normalize_index_daily(self, raw: pd.DataFrame, index_code: str, source: str) -> pd.DataFrame:
        cols = ["index_code", "trade_date", "open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "source", "updated_at"]
        if raw.empty:
            return pd.DataFrame(columns=cols)
        df = raw.rename(columns={
            "date": "trade_date", "日期": "trade_date", "code": "index_code", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "preclose": "preclose", "成交量": "volume", "成交额": "amount", "pctChg": "pct_chg", "涨跌幅": "pct_chg",
        }).copy()
        df["index_code"] = index_code
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        for col in ("open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg"):
            df[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else None
        df["source"] = source
        df["updated_at"] = pd.Timestamp.now()
        return df[cols]

    def _sleep_before_akshare(self) -> None:
        elapsed = time.monotonic() - self._last_akshare_request_at
        if elapsed < self.akshare_request_min_interval:
            time.sleep(self.akshare_request_min_interval - elapsed)
        time.sleep(random.uniform(self.request_sleep_min, self.request_sleep_max))
        self._last_akshare_request_at = time.monotonic()

    def _source_is_disabled(self, source: str) -> bool:
        normalized = self._normalize_source_name(source)
        disabled_until = self._source_disabled_until.get(normalized, 0.0)
        if disabled_until <= time.monotonic():
            self._source_disabled_until.pop(normalized, None)
            return False
        return True

    def _record_source_success(self, source: str) -> None:
        if not source:
            return
        normalized = self._normalize_source_name(source)
        self._source_failure_counts.pop(normalized, None)
        self._source_disabled_until.pop(normalized, None)

    def _record_source_failure(self, source: str, exc: Exception) -> None:
        if not source:
            return
        normalized = self._normalize_source_name(source)
        count = self._source_failure_counts.get(normalized, 0) + 1
        self._source_failure_counts[normalized] = count
        if count >= max(1, self.fallback_circuit_breaker_failures):
            self._source_disabled_until[normalized] = time.monotonic() + max(0, self.fallback_circuit_breaker_cooldown_seconds)
            logger.error(
                "source {} disabled by circuit breaker after {} consecutive failures; last_error={}",
                normalized,
                count,
                repr(exc),
            )

    def _configured_source(self, key: str, default: str) -> str:
        return self._normalize_source_name(str(self.source_cfg.get(key, default)))

    @staticmethod
    def _normalize_source_name(source: str) -> str:
        value = str(source).strip().lower()
        aliases = {
            "": "none",
            "ak": "akshare",
            "akshare.stock_zh_a_hist": "eastmoney",
            "akshare.stock_zh_a_hist_tx": "tencent",
            "akshare_tx_hist": "akshare_tx_hist",
            "akshare.index_zh_a_hist": "eastmoney",
            "akshare.stock_info_a_code_name": "akshare",
            "baostock.query_history_k_data_plus": "baostock",
            "baostock.query_history_k_data_plus.index": "baostock",
            "baostock.query_all_stock": "baostock",
            "eastmoney_public": "eastmoney",
            "eastmoney_history": "eastmoney",
            "tencent_history": "tencent",
            "westock_data": "westock",           # 新增
            "westock.data": "westock",            # 新增
            "westock-data.kline": "westock",      # 新增
            "akshare.stock_zh_a_hist_tx.qfq": "tencent",
            "akshare.stock_zh_a_hist_tx.hfq": "tencent",
            "akshare.stock_zh_a_hist_tx.bfq": "tencent",
            "sina": "sina",
            "akshare.stock_zh_index_daily": "sina",
        }
        return aliases.get(value, value)

    @staticmethod
    def _is_bse_code(code: str) -> bool:
        return str(code).lower().startswith("bj.")

    @staticmethod
    def _normalize_baostock_code(code: str) -> str:
        code = str(code)
        if code.startswith(("sz.", "sh.", "bj.")):
            return code
        if "." in code:
            symbol, exchange = code.split(".")
            return f"{exchange.lower()}.{symbol}"
        return MarketDataProvider._symbol_to_baostock_code(code)

    @staticmethod
    def _symbol_to_baostock_code(symbol: str) -> str:
        symbol = str(symbol).zfill(6)
        if symbol.startswith("6"):
            return f"sh.{symbol}"
        if symbol.startswith(("0", "1", "3")):
            # 0/3 开头：深市A股/创业板，1开头：深市ETF(159xxx)/LOF(16xxx)/REITs(18xxx)
            return f"sz.{symbol}"
        if symbol.startswith(("4", "8")):
            return f"bj.{symbol}"
        if symbol.startswith(("5")):  # 5开头：沪市基金/ETF，如 510xxx(ETF)、589xxx(专项)
            return f"sh.{symbol}"
        if symbol.startswith(("9")):
            return f"bj.{symbol}"
        return symbol

    @staticmethod
    def _to_iso_date(date_str: str) -> str:
        date_str = str(date_str)
        if "-" in date_str:
            return date_str
        return datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")

    @staticmethod
    def _to_yyyymmdd(date_str: str) -> str:
        date_str = str(date_str)
        if "-" in date_str:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")
        return date_str
