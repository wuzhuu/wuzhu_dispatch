from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

import aiohttp
import pandas as pd


SINA_SOURCE_NAME = "sina_spot_daily"
SINA_URL = "https://hq.sinajs.cn/list={symbols}"
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn/",
}


@dataclass
class SinaSpotConfig:
    batch_size: int = 80
    qps: float = 5.0
    max_concurrency: int = 10
    timeout: float = 10.0
    retries: int = 2
    retry_backoff_seconds: tuple[float, ...] = (1.0, 3.0)
    support_bse: bool = False


@dataclass
class SinaSpotResult:
    prices: pd.DataFrame
    missing: pd.DataFrame
    stats: dict[str, object]


class AsyncRateLimiter:
    def __init__(self, max_per_second: float) -> None:
        self.min_interval = 1.0 / max(max_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._last_at = 0.0
        self.request_starts: list[float] = []

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_seconds = self.min_interval - (now - self._last_at)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_at = time.monotonic()
            self.request_starts.append(self._last_at)


def to_sina_symbol(ts_code: str) -> tuple[str, str]:
    code = str(ts_code).strip()
    if "." in code:
        left, right = code.split(".", 1)
        if len(left) <= 3 and left.isalpha():
            exchange = left.lower()
            symbol = right.zfill(6)
        else:
            exchange = right.lower()
            symbol = left.zfill(6)
    else:
        symbol = code.zfill(6)
        if symbol.startswith("6"):
            exchange = "sh"
        elif symbol.startswith(("0", "3")):
            exchange = "sz"
        elif symbol.startswith(("4", "8", "9")):
            exchange = "bj"
        else:
            exchange = ""
    if not exchange:
        return code, code
    return f"{exchange}.{symbol}", f"{exchange}{symbol}"


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    size = max(1, size)
    for index in range(0, len(values), size):
        yield values[index : index + size]


def parse_sina_response(text: str, symbol_map: dict[str, str], target_date: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    prices: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    seen_symbols: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        head, _, payload = line.partition("=")
        sina_symbol = head.replace("var hq_str_", "").strip()
        seen_symbols.add(sina_symbol)
        ts_code = symbol_map.get(sina_symbol, sina_symbol)
        payload = payload.strip().strip(";").strip('"')
        fields = payload.split(",") if payload else []
        if len(fields) < 32 or not fields[0]:
            missing.append(_missing_row(target_date, ts_code, "EMPTY_RESPONSE"))
            continue

        row_date = fields[30].strip()
        if row_date != target_date:
            missing.append(_missing_row(target_date, ts_code, f"DATE_MISMATCH:{row_date or 'empty'}"))
            continue

        open_price = _to_float(fields[1])
        preclose = _to_float(fields[2])
        close = _to_float(fields[3])
        high = _to_float(fields[4])
        low = _to_float(fields[5])
        volume = _to_float(fields[8])
        amount = _to_float(fields[9])
        if all((value is None or value == 0) for value in (open_price, close, high, low)):
            missing.append(_missing_row(target_date, ts_code, "SUSPENDED_OR_EMPTY"))
            continue

        prices.append(
            {
                "ts_code": ts_code,
                "trade_date": pd.to_datetime(row_date).date(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "preclose": preclose,
                "volume": volume,
                "amount": amount,
                "pct_chg": _pct_chg(close, preclose),
                "turn": pd.NA,
                "tradestatus": "1",
                "is_st": pd.NA,
                "adjust_type": "qfq",
                "source": SINA_SOURCE_NAME,
                "updated_at": pd.Timestamp.now(),
            }
        )
    for sina_symbol, ts_code in symbol_map.items():
        if sina_symbol not in seen_symbols:
            missing.append(_missing_row(target_date, ts_code, "EMPTY_RESPONSE"))
    return prices, missing


async def fetch_sina_spot_daily(ts_codes: list[str], target_date: str, config: SinaSpotConfig | None = None) -> SinaSpotResult:
    config = config or SinaSpotConfig()
    symbols: list[str] = []
    symbol_map: dict[str, str] = {}
    missing_rows: list[dict[str, object]] = []
    for ts_code in ts_codes:
        normalized_code, sina_symbol = to_sina_symbol(ts_code)
        if sina_symbol.startswith("bj") and not config.support_bse:
            missing_rows.append(_missing_row(target_date, normalized_code, "UNSUPPORTED_MARKET"))
            continue
        symbols.append(sina_symbol)
        symbol_map[sina_symbol] = normalized_code

    batches = list(chunked(symbols, config.batch_size))
    limiter = AsyncRateLimiter(config.qps)
    semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
    request_latencies: list[float] = []
    request_errors: list[str] = []
    price_rows: list[dict[str, object]] = []
    started_at = pd.Timestamp.now()
    wall_start = time.monotonic()

    timeout = aiohttp.ClientTimeout(total=config.timeout)
    async with aiohttp.ClientSession(headers=SINA_HEADERS, timeout=timeout) as session:
        tasks = [
            _fetch_batch(session, batch, {symbol: symbol_map[symbol] for symbol in batch}, target_date, config, limiter, semaphore, request_latencies, request_errors)
            for batch in batches
        ]
        for prices, missing in await asyncio.gather(*tasks):
            price_rows.extend(prices)
            missing_rows.extend(missing)

    finished_at = pd.Timestamp.now()
    elapsed = max(time.monotonic() - wall_start, 0.001)
    expected_symbols = len(ts_codes)
    success_count = len(price_rows)
    missing_df = pd.DataFrame(missing_rows)
    prices_df = pd.DataFrame(price_rows)
    stats = {
        "source_name": SINA_SOURCE_NAME,
        "trade_date": target_date,
        "total_requests": len(batches),
        "success_count": success_count,
        "fail_count": max(expected_symbols - success_count, 0),
        "success_rate": success_count / expected_symbols if expected_symbols else 0.0,
        "avg_latency_ms": _mean(request_latencies) * 1000,
        "p95_latency_ms": _p95(request_latencies) * 1000,
        "actual_qps": len(batches) / elapsed,
        "peak_qps": _peak_qps(limiter.request_starts),
        "error_summary": _error_summary(request_errors, missing_df),
        "field_missing_rate": _field_missing_rate(prices_df),
        "date_mismatch_count": _fail_reason_count(missing_df, "DATE_MISMATCH"),
        "suspended_count": _fail_reason_count(missing_df, "SUSPENDED_OR_EMPTY"),
        "unsupported_market_count": _fail_reason_count(missing_df, "UNSUPPORTED_MARKET"),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    return SinaSpotResult(prices_df, missing_df, stats)


async def fetch_sina_spot_daily_streaming(
    ts_codes: list[str],
    target_date: str,
    config: SinaSpotConfig | None = None,
    flush_every_batches: int = 10,
    on_flush: Callable[[pd.DataFrame, pd.DataFrame, list[dict[str, object]]], None] | None = None,
    skip_batch_keys: set[str] | None = None,
) -> dict[str, object]:
    """Fetch Sina spot batches and flush completed results incrementally.

    This keeps the read/write cadence balanced: requests are still batched by
    symbol list, but completed batches are written every N batches instead of
    waiting for the entire market to finish.
    """
    config = config or SinaSpotConfig()
    symbols: list[str] = []
    symbol_map: dict[str, str] = {}
    pending_prices: list[dict[str, object]] = []
    pending_missing: list[dict[str, object]] = []
    pending_batches: list[dict[str, object]] = []
    all_missing: list[dict[str, object]] = []
    for ts_code in ts_codes:
        normalized_code, sina_symbol = to_sina_symbol(ts_code)
        if sina_symbol.startswith("bj") and not config.support_bse:
            row = _missing_row(target_date, normalized_code, "UNSUPPORTED_MARKET")
            pending_missing.append(row)
            all_missing.append(row)
            continue
        symbols.append(sina_symbol)
        symbol_map[sina_symbol] = normalized_code

    skip_batch_keys = skip_batch_keys or set()
    all_batches = [{"batch_key": _batch_key(batch), "symbols": batch} for batch in chunked(symbols, config.batch_size)]
    skipped_batches = [batch for batch in all_batches if batch["batch_key"] in skip_batch_keys]
    batches = [batch for batch in all_batches if batch["batch_key"] not in skip_batch_keys]
    skipped_symbol_count = sum(len(batch["symbols"]) for batch in skipped_batches)
    limiter = AsyncRateLimiter(config.qps)
    semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
    request_latencies: list[float] = []
    request_errors: list[str] = []
    started_at = pd.Timestamp.now()
    wall_start = time.monotonic()
    success_count = skipped_symbol_count
    completed_batches = 0
    batches_since_flush = 0
    field_missing_cells = 0
    field_total_cells = 0

    def flush() -> None:
        nonlocal pending_prices, pending_missing, pending_batches, batches_since_flush
        if not pending_prices and not pending_missing and not pending_batches:
            return
        prices_df = pd.DataFrame(pending_prices)
        missing_df = pd.DataFrame(pending_missing)
        if on_flush is not None:
            on_flush(prices_df, missing_df, pending_batches)
        pending_prices = []
        pending_missing = []
        pending_batches = []
        batches_since_flush = 0

    flush_every_batches = max(1, flush_every_batches)
    timeout = aiohttp.ClientTimeout(total=config.timeout)
    async with aiohttp.ClientSession(headers=SINA_HEADERS, timeout=timeout) as session:
        tasks = [
            asyncio.create_task(
                _fetch_batch_with_key(session, batch, symbol_map, target_date, config, limiter, semaphore, request_latencies, request_errors)
            )
            for batch in batches
        ]
        for task in asyncio.as_completed(tasks):
            batch_key, symbols_in_batch, prices, missing = await task
            completed_batches += 1
            batches_since_flush += 1
            success_count += len(prices)
            pending_prices.extend(prices)
            pending_missing.extend(missing)
            pending_batches.append(
                {
                    "trade_date": pd.to_datetime(target_date).date(),
                    "source_name": SINA_SOURCE_NAME,
                    "batch_key": batch_key,
                    "status": "completed",
                    "symbol_count": len(symbols_in_batch),
                    "price_rows": len(prices),
                    "missing_rows": len(missing),
                    "message": "",
                    "created_at": pd.Timestamp.now(),
                    "updated_at": pd.Timestamp.now(),
                }
            )
            all_missing.extend(missing)
            if prices:
                prices_df = pd.DataFrame(prices)
                cols = ["open", "high", "low", "close", "volume", "amount"]
                field_total_cells += len(prices_df) * len(cols)
                field_missing_cells += int(prices_df[cols].isna().sum().sum())
            if batches_since_flush >= flush_every_batches:
                flush()
    flush()

    finished_at = pd.Timestamp.now()
    elapsed = max(time.monotonic() - wall_start, 0.001)
    expected_symbols = len(ts_codes)
    missing_df = pd.DataFrame(all_missing)
    return {
        "source_name": SINA_SOURCE_NAME,
        "trade_date": target_date,
        "total_requests": len(batches),
        "skipped_batches": len(skipped_batches),
        "skipped_symbol_count": skipped_symbol_count,
        "success_count": success_count,
        "fail_count": max(expected_symbols - success_count, 0),
        "success_rate": success_count / expected_symbols if expected_symbols else 0.0,
        "avg_latency_ms": _mean(request_latencies) * 1000,
        "p95_latency_ms": _p95(request_latencies) * 1000,
        "actual_qps": len(batches) / elapsed,
        "peak_qps": _peak_qps(limiter.request_starts),
        "error_summary": _error_summary(request_errors, missing_df),
        "field_missing_rate": field_missing_cells / field_total_cells if field_total_cells else 0.0,
        "date_mismatch_count": _fail_reason_count(missing_df, "DATE_MISMATCH"),
        "suspended_count": _fail_reason_count(missing_df, "SUSPENDED_OR_EMPTY"),
        "unsupported_market_count": _fail_reason_count(missing_df, "UNSUPPORTED_MARKET"),
        "completed_batches": completed_batches,
        "flush_every_batches": flush_every_batches,
        "started_at": started_at,
        "finished_at": finished_at,
    }


async def _fetch_batch_with_key(
    session: aiohttp.ClientSession,
    batch_info: dict[str, object],
    symbol_map: dict[str, str],
    target_date: str,
    config: SinaSpotConfig,
    limiter: AsyncRateLimiter,
    semaphore: asyncio.Semaphore,
    request_latencies: list[float],
    request_errors: list[str],
) -> tuple[str, list[str], list[dict[str, object]], list[dict[str, object]]]:
    symbols = list(batch_info["symbols"])
    batch_key = str(batch_info["batch_key"])
    prices, missing = await _fetch_batch(session, symbols, {symbol: symbol_map[symbol] for symbol in symbols}, target_date, config, limiter, semaphore, request_latencies, request_errors)
    return batch_key, symbols, prices, missing


async def _fetch_batch(
    session: aiohttp.ClientSession,
    batch: list[str],
    symbol_map: dict[str, str],
    target_date: str,
    config: SinaSpotConfig,
    limiter: AsyncRateLimiter,
    semaphore: asyncio.Semaphore,
    request_latencies: list[float],
    request_errors: list[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    url = SINA_URL.format(symbols=",".join(batch))
    async with semaphore:
        for attempt in range(config.retries + 1):
            await limiter.wait()
            start = time.monotonic()
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    raw = await response.read()
                    request_latencies.append(time.monotonic() - start)
                    text = raw.decode("gbk", errors="ignore")
                    return parse_sina_response(text, symbol_map, target_date)
            except Exception as exc:
                request_errors.append(type(exc).__name__)
                if attempt >= config.retries:
                    return [], [_missing_row(target_date, symbol_map.get(symbol, symbol), f"REQUEST_FAILED:{exc!r}") for symbol in batch]
                backoff = config.retry_backoff_seconds[min(attempt, len(config.retry_backoff_seconds) - 1)]
                await asyncio.sleep(backoff)
    return [], []


def _missing_row(trade_date: str, code: str, fail_reason: str) -> dict[str, object]:
    now = pd.Timestamp.now()
    return {
        "trade_date": pd.to_datetime(trade_date).date(),
        "code": code,
        "source_name": SINA_SOURCE_NAME,
        "fail_reason": fail_reason,
        "retry_count": 0,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }


def _batch_key(symbols: list[str]) -> str:
    payload = ",".join(symbols)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _to_float(value: str) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    return parsed


def _pct_chg(close: float | None, preclose: float | None) -> float | None:
    if close is None or preclose in (None, 0):
        return None
    return (close - preclose) / preclose * 100


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return ordered[index]


def _peak_qps(starts: list[float]) -> float:
    if not starts:
        return 0.0
    peak = 0
    for index, started_at in enumerate(starts):
        count = sum(1 for value in starts[index:] if value - started_at <= 1.0)
        peak = max(peak, count)
    return float(peak)


def _fail_reason_count(df: pd.DataFrame, token: str) -> int:
    if df.empty or "fail_reason" not in df.columns:
        return 0
    return int(df["fail_reason"].astype(str).str.startswith(token).sum())


def _field_missing_rate(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    cols = ["open", "high", "low", "close", "volume", "amount"]
    total = len(df) * len(cols)
    missing = int(df[cols].isna().sum().sum())
    return missing / total if total else 0.0


def _error_summary(request_errors: list[str], missing_df: pd.DataFrame) -> str:
    counts: dict[str, int] = {}
    for value in request_errors:
        counts[value] = counts.get(value, 0) + 1
    if not missing_df.empty and "fail_reason" in missing_df.columns:
        for reason in missing_df["fail_reason"].astype(str):
            key = reason.split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
