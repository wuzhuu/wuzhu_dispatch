from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.collectors.market_provider import MarketDataProvider
from src.collectors.rss_news_collector import run_ingest as run_rss_news_ingest
from src.storage.lake_store import LakeStore
from src.utils.calendar import is_trade_day
from src.utils.config import ensure_project_dirs, load_settings, resolve_db_path, resolve_log_root
from src.utils.logger import setup_logger
from src.utils.proxy import disable_proxy_if_configured
from src.jobs.quality_check import run_quality_checks


INDEX_CODES = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]
RECENT_GAP_TASK_NAME = "recent_gap_daily_price"
RECENT_INDEX_GAP_TASK_NAME = "recent_gap_index_daily"
DAILY_PRICE_BATCH_TASK_NAME = "daily_price_batch_write"
SINA_SPOT_TASK_NAME = "sina_spot_daily"
STOCK_BASIC_TASK_NAME = "stock_basic"
INDUSTRY_BOARD_TASK_NAME = "industry_board_local"
STOCK_INDUSTRY_MAP_TASK_NAME = "stock_industry_map"


def _target_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _next_day(date_value) -> str:
    """Return the next trading day (weekday check, no network calls)."""
    dt = pd.to_datetime(date_value).to_pydatetime().date() + timedelta(days=1)
    while dt.weekday() >= 5:  # Skip Sat/Sun without network calls
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _previous_day(date_value: str) -> str:
    """Return the previous trading day (weekday check, no network calls)."""
    dt = pd.to_datetime(date_value).to_pydatetime().date() - timedelta(days=1)
    while dt.weekday() >= 5:  # Skip Sat/Sun without network calls
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _log_update(store: LakeStore, task_name: str, target: str, status: str, message: str) -> None:
    store.execute(
        "INSERT INTO daily_update_log (log_time, task_name, target, status, message) VALUES (?, ?, ?, ?, ?)",
        [pd.Timestamp.now(), task_name, target, status, message],
    )


def _max_trade_date(store: LakeStore, ts_code: str) -> str | None:
    row = store.execute("SELECT MAX(trade_date) FROM v_daily_price WHERE ts_code = ?", [ts_code]).fetchone()
    return row[0] if row and row[0] is not None else None


def _recent_trade_dates(target_date: str, lookback_days: int) -> list[str]:
    target = pd.to_datetime(target_date).date()
    start = target - timedelta(days=max(lookback_days - 1, 0))
    dates: list[str] = []
    current = start
    while current <= target:
        day = current.strftime("%Y-%m-%d")
        if is_trade_day(current.strftime("%Y%m%d")):
            dates.append(day)
        current += timedelta(days=1)
    return dates


def _existing_pairs(store: LakeStore, view_name: str, symbol_col: str, symbols: list[str], trade_dates: list[str]) -> set[tuple[str, str]]:
    if not symbols or not trade_dates:
        return set()
    start_date, end_date = min(trade_dates), max(trade_dates)
    rows = store.execute(
        f"""
        SELECT DISTINCT {symbol_col}, CAST(trade_date AS VARCHAR)
        FROM {view_name}
        WHERE trade_date BETWEEN ? AND ?
        """,
        [start_date, end_date],
    ).fetchall()
    symbol_set = set(symbols)
    date_set = set(trade_dates)
    return {(str(symbol), str(trade_date)) for symbol, trade_date in rows if str(symbol) in symbol_set and str(trade_date) in date_set}


def _missing_dates_by_symbol(store: LakeStore, view_name: str, symbol_col: str, symbols: list[str], trade_dates: list[str]) -> dict[str, list[str]]:
    existing = _existing_pairs(store, view_name, symbol_col, symbols, trade_dates)
    missing: dict[str, list[str]] = {}
    for symbol in symbols:
        dates = [date for date in trade_dates if (symbol, date) not in existing]
        if dates:
            missing[symbol] = dates
    return missing


def _date_ranges(trade_dates: list[str]) -> list[tuple[str, str]]:
    if not trade_dates:
        return []
    dates = sorted(pd.to_datetime(trade_dates).date)
    ranges: list[tuple[str, str]] = []
    start = prev = dates[0]
    for current in dates[1:]:
        if (current - prev).days == 1:
            prev = current
            continue
        ranges.append((start.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")))
        start = prev = current
    ranges.append((start.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")))
    return ranges


def _flush_daily_price_batch(store: LakeStore, frames: list[pd.DataFrame]) -> int:
    if not frames:
        return 0
    batch = pd.concat(frames, ignore_index=True)
    if batch.empty:
        frames.clear()
        return 0
    rows = store.upsert_daily_price(batch)
    frames.clear()
    return rows


def _safe_flush_daily_price_batch(store: LakeStore, frames: list[pd.DataFrame], target: str) -> int:
    if not frames:
        return 0
    batch_count = len(frames)
    try:
        rows = _flush_daily_price_batch(store, frames)
        _log_update(store, DAILY_PRICE_BATCH_TASK_NAME, target, "OK", f"frames={batch_count} rows={rows}")
        return rows
    except Exception as exc:
        logger.exception("daily_price batch write failed target={}", target)
        frames.clear()
        _log_update(store, DAILY_PRICE_BATCH_TASK_NAME, target, "FAIL", repr(exc))
        return 0


def _after_formal_daily_close(target_date: str, close_time: str = "15:10") -> bool:
    target = pd.to_datetime(target_date).date()
    today = datetime.now().date()
    if target < today:
        return True
    if target > today:
        return False
    hour, minute = [int(part) for part in close_time.split(":", 1)]
    return datetime.now().time() >= time(hour, minute)


def _sina_config_from_settings(settings: dict[str, Any]):
    from src.collectors.sina_spot_daily import SinaSpotConfig

    collector = settings.get("collector", {})
    return SinaSpotConfig(
        batch_size=int(collector.get("sina_batch_size", 80)),
        qps=float(collector.get("sina_qps", 5)),
        max_concurrency=int(collector.get("sina_max_concurrency", 10)),
        timeout=float(collector.get("sina_timeout", 10)),
        retries=int(collector.get("sina_retries", 2)),
    )


def _collect_sina_spot_daily(
    store: LakeStore,
    settings: dict[str, Any],
    ts_codes: list[str],
    target_date: str,
    dry_run: bool = False,
) -> tuple[int, int, dict[str, object]]:
    try:
        from src.collectors.sina_spot_daily import SINA_SOURCE_NAME, fetch_sina_spot_daily_streaming
    except Exception as exc:
        _log_update(store, SINA_SPOT_TASK_NAME, target_date, "FAIL", f"import failed: {exc!r}")
        return 0, 0, {"source_name": SINA_SPOT_TASK_NAME, "error_summary": f"import failed: {exc!r}"}

    formal_close_time = str(settings.get("collector", {}).get("formal_daily_close_time", "15:10"))
    allow_formal_write = _after_formal_daily_close(target_date, formal_close_time)
    write_temp_before_close = bool(settings.get("collector", {}).get("sina_write_temp_before_close", True))
    flush_every_batches = int(settings.get("collector", {}).get("sina_write_flush_batches", 10))
    resume_enabled = bool(settings.get("collector", {}).get("sina_resume_enabled", True))
    completed_batch_keys = store.completed_collection_batches(target_date, SINA_SOURCE_NAME) if resume_enabled and not dry_run else set()
    rows_written = 0
    temp_rows = 0
    missing_rows = 0
    flush_count = 0

    def on_flush(prices: pd.DataFrame, missing: pd.DataFrame, batches: list[dict[str, object]]) -> None:
        nonlocal rows_written, temp_rows, missing_rows, flush_count
        flush_count += 1
        if dry_run:
            rows_written += len(prices) if allow_formal_write else 0
            temp_rows += len(prices) if not allow_formal_write and write_temp_before_close else 0
            missing_rows += len(missing)
            return
        if not missing.empty:
            missing_rows += store.upsert_missing_daily_price(missing)
        if not prices.empty and allow_formal_write:
            rows_written += store.upsert_daily_price(prices)
        elif not prices.empty and write_temp_before_close:
            temp_rows += store.upsert_daily_price_temp(prices, status="intraday")
        if batches:
            store.upsert_collection_checkpoint(pd.DataFrame(batches))

    stats = asyncio.run(
        fetch_sina_spot_daily_streaming(
            ts_codes,
            target_date,
            _sina_config_from_settings(settings),
            flush_every_batches=flush_every_batches,
            on_flush=on_flush,
            skip_batch_keys=completed_batch_keys,
        )
    )
    if not dry_run:
        store.record_data_source_metrics(stats)

    status = "OK" if int(stats.get("success_count", 0)) > 0 else "FAIL"
    _log_update(
        store,
        SINA_SPOT_TASK_NAME,
        target_date,
        status,
        (
            f"source={SINA_SOURCE_NAME} prices={int(stats.get('success_count', 0))} missing={missing_rows} "
            f"formal_write={allow_formal_write} rows={rows_written} temp_rows={temp_rows} flushes={flush_count} "
            f"resumed_batches={len(completed_batch_keys)} skipped_batches={int(stats.get('skipped_batches', 0))} "
            f"actual_qps={float(stats.get('actual_qps', 0.0)):.2f} peak_qps={float(stats.get('peak_qps', 0.0)):.2f} "
            f"success_rate={float(stats.get('success_rate', 0.0)):.4f}"
        ),
    )
    return rows_written, temp_rows, stats


def _collect_stock_basic(
    store: LakeStore,
    provider: MarketDataProvider,
    target_date: str,
    dry_run: bool = False,
) -> tuple[pd.DataFrame, int, str]:
    try:
        stock_basic = provider.get_stock_basic()
        source = provider.last_run.source if provider.last_run else ""
        fallback_used = bool(provider.last_run.fallback_used) if provider.last_run else False
        rows = len(stock_basic)
        if rows == 0:
            message = f"rows=0 source={source} fallback_used={fallback_used}"
            raise RuntimeError(f"stock_basic returned empty dataframe: {message}")
        written_rows = rows if dry_run else store.write_stock_basic_snapshot(stock_basic, snapshot_date=target_date)
        message = f"rows={rows} written={written_rows} source={source} fallback_used={fallback_used} dry_run={dry_run}"
        _log_update(store, STOCK_BASIC_TASK_NAME, target_date, "OK", message)
        return stock_basic, written_rows, message
    except Exception as exc:
        logger.exception("stock_basic pipeline failed")
        _log_update(store, STOCK_BASIC_TASK_NAME, target_date, "FAIL", repr(exc))
        raise


def _collect_industry_boards(store: LakeStore, target_date: str, dry_run: bool = False) -> tuple[int, str]:
    return _build_industry_board_local(store, target_date, dry_run=dry_run)


def _collect_stock_industry_map(store: LakeStore, stock_basic: pd.DataFrame, target_date: str, dry_run: bool = False) -> tuple[int, str]:
    try:
        from scripts.update_stock_industry_map import fetch_baostock_industry_map

        industry_map = fetch_baostock_industry_map()
        if not industry_map.empty and "ts_code" in stock_basic.columns:
            names = stock_basic[[c for c in ["ts_code", "name"] if c in stock_basic.columns]].drop_duplicates("ts_code")
            industry_map = industry_map.merge(names, on="ts_code", how="left", suffixes=("", "_basic"))
            if "name_basic" in industry_map:
                industry_map["name"] = industry_map["name"].fillna(industry_map["name_basic"])
                industry_map = industry_map.drop(columns=["name_basic"])
        rows = len(industry_map)
        if rows == 0:
            message = f"rows=0 error={industry_map.attrs.get('error', '')}"
            _log_update(store, STOCK_INDUSTRY_MAP_TASK_NAME, target_date, "WARN", message)
            return 0, message
        written_rows = rows if dry_run else store.upsert_stock_industry_map(industry_map)
        source_counts = industry_map["source"].value_counts().to_dict() if "source" in industry_map.columns else {}
        message = f"rows={rows} written={written_rows} sources={source_counts} dry_run={dry_run}"
        _log_update(store, STOCK_INDUSTRY_MAP_TASK_NAME, target_date, "OK", message)
        return written_rows, message
    except Exception as exc:
        logger.exception("stock_industry_map pipeline failed")
        _log_update(store, STOCK_INDUSTRY_MAP_TASK_NAME, target_date, "FAIL", repr(exc))
        return 0, f"failed: {exc!r}"


def _build_industry_board_local(store: LakeStore, target_date: str, dry_run: bool = False) -> tuple[int, str]:
    try:
        from scripts.build_industry_board_local import build_industry_board

        board, summary, warnings = build_industry_board(store, target_date, target_date, "qfq", 3)
        if board.empty:
            message = "rows=0 source=local_aggregate"
            if warnings:
                message += f" warnings={warnings}"
            _log_update(store, INDUSTRY_BOARD_TASK_NAME, target_date, "WARN", message)
            return 0, message
        written_rows = len(board) if dry_run else store.write_industry_board_local(board)
        summary_json = summary.to_json(orient="records", force_ascii=False) if not summary.empty else "[]"
        message = f"rows={len(board)} written={written_rows} source=local_aggregate dry_run={dry_run} summary={summary_json}"
        if warnings:
            message += f" warnings={warnings}"
        _log_update(store, INDUSTRY_BOARD_TASK_NAME, target_date, "OK", message)
        return written_rows, message
    except Exception as exc:
        logger.exception("industry_board_local pipeline failed")
        _log_update(store, INDUSTRY_BOARD_TASK_NAME, target_date, "WARN", repr(exc))
        return 0, f"failed: {exc!r}"


def _report_path(settings: dict[str, Any], target_date: str) -> Path:
    log_root = resolve_log_root(settings)
    log_root.mkdir(parents=True, exist_ok=True)
    return log_root / f"daily_report_{target_date.replace('-', '')}.md"


def _write_daily_report(settings: dict[str, Any], target_date: str, rows: list[str], quality_df: pd.DataFrame) -> Path:
    path = _report_path(settings, target_date)
    lines = ["# Daily Update Report", "", f"- target_date: {target_date}", f"- generated_at: {datetime.now():%Y-%m-%d %H:%M:%S}", "", "## Updates", "", *rows, "", "## Quality", "", quality_df.to_markdown(index=False) if not quality_df.empty else "No quality rows."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _backfill_recent_daily_gaps(
    store: LakeStore,
    provider: MarketDataProvider,
    ts_codes: list[str],
    target_date: str,
    lookback_days: int,
    write_batch_size: int,
    sample_mode: bool = False,
) -> tuple[int, int, int]:
    trade_dates = _recent_trade_dates(target_date, lookback_days)
    if not trade_dates:
        return 0, 0, 0
    missing = _missing_dates_by_symbol(store, "v_daily_price", "ts_code", ts_codes, trade_dates)
    missing_symbols = len(missing)
    missing_pairs = sum(len(dates) for dates in missing.values())
    rows_written = 0
    frames: list[pd.DataFrame] = []
    for ts_code, dates in missing.items():
        for start_date, end_date in _date_ranges(dates):
            try:
                df = provider.get_daily_price(ts_code, start_date, end_date)
                if not df.empty:
                    wanted = set(dates)
                    df = df[pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").isin(wanted)]
                if not df.empty:
                    frames.append(df)
                    rows_written += len(df)
                    if len(frames) >= write_batch_size:
                        _safe_flush_daily_price_batch(store, frames, RECENT_GAP_TASK_NAME)
                _log_update(store, RECENT_GAP_TASK_NAME, ts_code, "OK", f"range={start_date}:{end_date} missing_dates={len(dates)} rows={len(df)} source={provider.last_run.source if provider.last_run else ''}")
            except Exception as exc:
                logger.exception("recent daily gap backfill failed for {}", ts_code)
                _log_update(store, RECENT_GAP_TASK_NAME, ts_code, "FAIL", f"range={start_date}:{end_date} {exc!r}")
        if sample_mode and rows_written:
            break
    _safe_flush_daily_price_batch(store, frames, RECENT_GAP_TASK_NAME)
    return rows_written, missing_symbols, missing_pairs


def _backfill_recent_index_gaps(store: LakeStore, provider: MarketDataProvider, target_date: str, lookback_days: int) -> tuple[int, int, int]:
    trade_dates = _recent_trade_dates(target_date, lookback_days)
    if not trade_dates:
        return 0, 0, 0
    missing = _missing_dates_by_symbol(store, "v_index_daily", "index_code", INDEX_CODES, trade_dates)
    missing_symbols = len(missing)
    missing_pairs = sum(len(dates) for dates in missing.values())
    rows_written = 0
    for index_code, dates in missing.items():
        for start_date, end_date in _date_ranges(dates):
            try:
                df = provider.get_index_daily(index_code, start_date, end_date)
                if not df.empty:
                    wanted = set(dates)
                    df = df[pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").isin(wanted)]
                if not df.empty:
                    store.upsert_index_daily(df)
                    rows_written += len(df)
                _log_update(store, RECENT_INDEX_GAP_TASK_NAME, index_code, "OK", f"range={start_date}:{end_date} missing_dates={len(dates)} rows={len(df)} source={provider.last_run.source if provider.last_run else ''}")
            except Exception as exc:
                logger.exception("recent index gap backfill failed for {}", index_code)
                _log_update(store, RECENT_INDEX_GAP_TASK_NAME, index_code, "FAIL", f"range={start_date}:{end_date} {exc!r}")
    return rows_written, missing_symbols, missing_pairs


def _apply_runtime_overrides(settings: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    if not overrides:
        return settings
    merged = dict(settings)
    for section, values in overrides.items():
        if not isinstance(values, dict):
            merged[section] = values
            continue
        current = dict(merged.get(section, {}))
        current.update({key: value for key, value in values.items() if value is not None})
        merged[section] = current
    return merged


def _repair_missing_daily_price(store: LakeStore, provider: MarketDataProvider, target_date: str | None = None) -> tuple[int, int]:
    where = "WHERE status = 'pending'"
    params: list[object] = []
    if target_date:
        where += " AND trade_date = ?"
        params.append(pd.to_datetime(target_date).date())
    rows = store.execute(
        f"""
        SELECT code, CAST(trade_date AS VARCHAR)
        FROM missing_daily_price
        {where}
        ORDER BY code, trade_date
        """,
        params,
    ).fetchall()
    if not rows:
        return 0, 0

    by_code: dict[str, list[str]] = {}
    for code, trade_date in rows:
        by_code.setdefault(str(code), []).append(str(trade_date))

    repaired_codes: list[str] = []
    failed = 0
    for code, dates in by_code.items():
        for start_date, end_date in _date_ranges(dates):
            try:
                df = provider.get_daily_price(code, start_date, end_date)
                if not df.empty:
                    wanted = set(dates)
                    df = df[pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").isin(wanted)]
                if df.empty:
                    failed += len(dates)
                    store.execute(
                        """
                        UPDATE missing_daily_price
                        SET retry_count = retry_count + 1, fail_reason = ?, updated_at = ?
                        WHERE code = ? AND trade_date BETWEEN ? AND ? AND status = 'pending'
                        """,
                        ["baostock_hist_empty", pd.Timestamp.now(), code, start_date, end_date],
                    )
                    continue
                store.upsert_daily_price(df)
                repaired_codes.extend(df["ts_code"].dropna().astype(str).unique().tolist())
                store.execute(
                    """
                    UPDATE missing_daily_price
                    SET status = 'repaired', updated_at = ?
                    WHERE code = ? AND trade_date BETWEEN ? AND ? AND status = 'pending'
                    """,
                    [pd.Timestamp.now(), code, start_date, end_date],
                )
            except Exception as exc:
                failed += len(dates)
                logger.exception("repair missing daily_price failed for {}", code)
                store.execute(
                    """
                    UPDATE missing_daily_price
                    SET retry_count = retry_count + 1, fail_reason = ?, updated_at = ?
                    WHERE code = ? AND trade_date BETWEEN ? AND ? AND status = 'pending'
                    """,
                    [repr(exc), pd.Timestamp.now(), code, start_date, end_date],
                )
    return len(set(repaired_codes)), failed


def run_daily_update(
    target_date: str | None = None,
    sample_mode: bool = False,
    db_path: str | None = None,
    dry_run: bool = False,
    repair_missing: bool = False,
    settings_overrides: dict[str, Any] | None = None,
) -> None:
    disable_proxy_if_configured()
    settings = _apply_runtime_overrides(load_settings(), settings_overrides)
    ensure_project_dirs(settings)
    setup_logger("daily_update.log")
    target_date = target_date or _target_date()
    provider = MarketDataProvider()
    store = LakeStore(db_path=resolve_db_path(settings, db_path))
    report_rows: list[str] = []

    try:
        stock_basic, _, stock_basic_message = _collect_stock_basic(store, provider, target_date, dry_run=dry_run)
        report_rows.append(f"- stock_basic {stock_basic_message}")

        _, stock_industry_message = _collect_stock_industry_map(store, stock_basic, target_date, dry_run=dry_run)
        report_rows.append(f"- stock_industry_map {stock_industry_message}")

        report_rows.append("- industry_board_local pending: built after daily_price updates")

        try:
            rss_summary = run_rss_news_ingest(db_path=store.db_path, settings=settings)
            report_rows.append(
                "- rss_news "
                f"rows: {int(rss_summary.get('new_rows', 0))} "
                f"sources: {int(rss_summary.get('fetched_sources', 0))} "
                f"failed: {len(rss_summary.get('failed', []))} "
                f"skipped: {int(rss_summary.get('skipped_sources', 0))}"
            )
        except Exception as exc:
            logger.exception("rss_news failed")
            report_rows.append(f"- rss_news failed: {exc!r}")

        if not is_trade_day(target_date.replace("-", "")):
            logger.info("{} is not a trade day. Exit.", target_date)
            report_rows.append("- market update skipped: non-trade day")
            quality_df = pd.DataFrame()
            path = _write_daily_report(settings, target_date, report_rows, quality_df)
            logger.info("daily report written: {}", path)
            return

        start_default = settings.get("collector", {}).get("backfill_start_date", "2018-01-01")
        max_stocks = int(settings.get("collector", {}).get("daily_price_max_stocks", 0))
        ts_codes = stock_basic["ts_code"].dropna().astype(str).tolist()
        if sample_mode:
            ts_codes = ["sz.000001", "sh.600000", "sz.300750"]
        elif max_stocks > 0:
            ts_codes = ts_codes[:max_stocks]

        current_source = str(settings.get("data_source", {}).get("daily_price_current_primary", "")).lower()
        uses_sina_spot = current_source == "sina_spot_daily"
        if uses_sina_spot:
            try:
                sina_rows, sina_temp_rows, sina_stats = _collect_sina_spot_daily(store, settings, ts_codes, target_date, dry_run=dry_run)
                report_rows.append(
                    "- sina_spot_daily "
                    f"rows: {sina_rows} temp_rows: {sina_temp_rows} "
                    f"success_rate: {float(sina_stats.get('success_rate', 0.0)):.4f} "
                    f"actual_qps: {float(sina_stats.get('actual_qps', 0.0)):.2f} "
                    f"peak_qps: {float(sina_stats.get('peak_qps', 0.0)):.2f} "
                    f"date_mismatch: {int(sina_stats.get('date_mismatch_count', 0))} "
                    f"suspended: {int(sina_stats.get('suspended_count', 0))} "
                    f"unsupported_market: {int(sina_stats.get('unsupported_market_count', 0))}"
                )
            except Exception as exc:
                logger.exception("sina_spot_daily failed")
                _log_update(store, SINA_SPOT_TASK_NAME, target_date, "FAIL", repr(exc))
                report_rows.append(f"- sina_spot_daily failed: {exc!r}")

        history_end_date = _previous_day(target_date) if uses_sina_spot else target_date
        write_batch_size = max(1, int(settings.get("collector", {}).get("daily_price_write_batch_size", 200)))
        total_rows = 0
        daily_frames: list[pd.DataFrame] = []
        
        # 一次性查询所有股票的最大日期，避免逐只查询
        logger.info("Querying max trade dates for all stocks...")
        max_dates_map: dict[str, str | None] = {}
        try:
            rows = store.execute("""
                SELECT ts_code, MAX(trade_date) as max_date
                FROM v_daily_price
                GROUP BY ts_code
            """).fetchall()
            for row in rows:
                max_dates_map[row[0]] = str(row[1])[:10] if row[1] else None
            logger.info("Queried max dates for {} stocks", len(max_dates_map))
        except Exception as exc:
            logger.warning("Failed to batch query max dates, falling back to per-stock query: {}", exc)
        
        history_end_dt = pd.to_datetime(history_end_date)
        # 1. 筛选需要采集的股票（跳过已最新）
        need_fetch: list[tuple[str, str, str]] = []  # (ts_code, start_date, source_hint)
        skip_count = 0
        for ts_code in ts_codes:
            if ts_code in max_dates_map:
                max_date = max_dates_map[ts_code]
            else:
                max_date = _max_trade_date(store, ts_code)
            start_date = _next_day(max_date) if max_date else start_default
            if pd.to_datetime(start_date) > history_end_dt:
                skip_count += 1
                continue
            need_fetch.append((ts_code, start_date, history_end_date))
        logger.info("daily_price_history: {} stocks up-to-date (SKIP), {} stocks need fetch", skip_count, len(need_fetch))

        # 2. 分流：股票交替分配到 baostock（串行+锁）和 tencent（并行无锁）两组
        #    两组同时跑，整体吞吐提升；一组卡住不影响另一组
        def _source_for_code(ts_code: str, idx: int) -> str:
            """全走 baostock（主源已修复），tencent 仅在重试阶段做备用"""
            return "baostock"

        groups: dict[str, list[tuple[str, str, str]]] = {}
        for idx, item in enumerate(need_fetch):
            source = _source_for_code(item[0], idx)
            groups.setdefault(source, []).append(item)
        logger.info("daily_price_history: groups={}", {k: len(v) for k, v in groups.items()})

        # 所有可用源（按优先级排序）
        # baostock 为主源（须通过 _BAOSTOCK_LOCK 串行访问），tencent 为备用
        ALL_SOURCES = ["baostock", "tencent"]

        # 3. 并行采集：每组一个线程（baostock 不支持并发，单线程采集）
        def _fetch_group(group_source: str, stock_list: list[tuple[str, str, str]]) -> tuple[list[pd.DataFrame], list[str]]:
            """采集一个组的股票，返回 (frames, failed_codes)

            源亲和策略：同一只股票成功后，下一只优先用同一个源；
            源失败后降级尝试其他源，找到能用的就切换过去。
            """
            local_provider = MarketDataProvider()
            local_frames: list[pd.DataFrame] = []
            failed: list[str] = []
            preferred = group_source.replace("_sz", "").replace("_sh", "").replace("_bj", "")  # strip market suffix for actual source name
            for ts_code, s_date, e_date in stock_list:
                try:
                    # ① 快速路径：只用偏好源尝试一次
                    df = local_provider.get_daily_price_multi_source(ts_code, s_date, e_date, [preferred])
                    if df.empty:
                        # ② 偏好源返回空 → 这只股票本来源就没数据（退市/停牌）
                        #    不降级：所有源覆盖同一市场，其他源也查不到
                        #    只有源本身异常（ConnectionError等）才说明源挂了
                        failed.append(ts_code)
                    else:
                        local_frames.append(df)
                        # 提取实际成功的 source 作为下次偏好源
                        if "source" in df.columns:
                            preferred = df["source"].iloc[0]
                except Exception as exc:
                    logger.exception("daily_price parallel fetch failed for {}", ts_code)
                    failed.append(ts_code)
            local_provider.close_baostock_session()
            return local_frames, failed

        total_rows = 0
        all_failed: list[str] = []
        if need_fetch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(groups), 4)) as executor:
                fut_map = {executor.submit(_fetch_group, src, stocks): src for src, stocks in groups.items()}
                for future in concurrent.futures.as_completed(fut_map):
                    src = fut_map[future]
                    try:
                        frames, failed = future.result()
                        for df in frames:
                            daily_frames.append(df)
                            total_rows += len(df)
                            if len(daily_frames) >= write_batch_size:
                                _safe_flush_daily_price_batch(store, daily_frames, "daily_price")
                        all_failed.extend(failed)
                        if failed:
                            logger.warning("daily_price group {} had {} failures", src, len(failed))
                    except Exception as exc:
                        logger.exception("daily_price group {} failed: {}", src, exc)
                        all_failed.extend([c for c, _, _ in groups.get(src, [])])

            # 4. 重试失败的股票（尝试所有可用源）
            if all_failed:
                logger.info("Retrying {} failed stocks with all sources...", len(all_failed))
                retry_provider = MarketDataProvider()
                for ts_code in all_failed:
                    try:
                        df = retry_provider.get_daily_price_multi_source(
                            ts_code, start_default, history_end_date, ALL_SOURCES
                        )
                        if not df.empty:
                            daily_frames.append(df)
                            total_rows += len(df)
                            if len(daily_frames) >= write_batch_size:
                                _safe_flush_daily_price_batch(store, daily_frames, "daily_price")
                    except Exception as exc:
                        logger.exception("daily_price retry failed for {}", ts_code)
                        _log_update(store, "daily_price", ts_code, "FAIL", repr(exc))
                retry_provider.close_baostock_session()

        _safe_flush_daily_price_batch(store, daily_frames, "daily_price")
        report_rows.append(f"- daily_price_history rows: {total_rows} end_date: {history_end_date}")

        index_rows = 0
        for index_code in INDEX_CODES:
            try:
                df = provider.get_index_daily(index_code, target_date, target_date)
                if not df.empty:
                    store.upsert_index_daily(df)
                    index_rows += len(df)
                _log_update(store, "index_daily", index_code, "OK", f"rows={len(df)} source={provider.last_run.source if provider.last_run else ''}")
            except Exception as exc:
                logger.exception("index_daily failed for {}", index_code)
                _log_update(store, "index_daily", index_code, "FAIL", repr(exc))
        report_rows.append(f"- index_daily rows: {index_rows}")

        recent_gap_days = int(settings.get("collector", {}).get("recent_gap_lookback_days", 7))
        recent_rows, recent_symbols, recent_pairs = _backfill_recent_daily_gaps(store, provider, ts_codes, target_date, recent_gap_days, write_batch_size, sample_mode=sample_mode)
        recent_index_rows, recent_index_symbols, recent_index_pairs = _backfill_recent_index_gaps(store, provider, target_date, recent_gap_days)
        report_rows.append(f"- recent_gap_daily_price rows: {recent_rows} missing_symbols: {recent_symbols} missing_pairs: {recent_pairs} lookback_days: {recent_gap_days}")
        report_rows.append(f"- recent_gap_index_daily rows: {recent_index_rows} missing_symbols: {recent_index_symbols} missing_pairs: {recent_index_pairs} lookback_days: {recent_gap_days}")

        if repair_missing and not dry_run:
            repaired_symbols, repair_failed = _repair_missing_daily_price(store, provider, target_date)
            report_rows.append(f"- repair_missing_daily_price repaired_symbols: {repaired_symbols} failed_rows: {repair_failed}")
        elif repair_missing and dry_run:
            report_rows.append("- repair_missing_daily_price skipped: dry_run")

        _, industry_board_message = _collect_industry_boards(store, target_date, dry_run=dry_run)
        report_rows.append(f"- industry_board_local {industry_board_message}")

        quality_df = run_quality_checks(store, target_date, sample_mode=sample_mode)
        path = _write_daily_report(settings, target_date, report_rows, quality_df)
        logger.info("daily report written: {}", path)
    finally:
        provider.close_baostock_session()
        store.close()
