from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import load_recommendation_config
from .stores import RecommendationStore


def update_tracking(
    db_path: str | Path | None,
    config_path: str | Path | None,
    target_date: str | None = None,
    batch_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    cfg = load_recommendation_config(config_path)
    rec = RecommendationStore(db_path)
    try:
        rec.refresh()
        batches = _open_batches(rec, batch_id)
        if batches.empty:
            return ["status: NO_OPEN_BATCH"]
        items = rec.read_dataset("recommendation_item")
        if batch_id:
            items = items[items["batch_id"] == batch_id]
        else:
            open_batch_ids = set(batches["batch_id"].astype(str))
            items = items[items["batch_id"].astype(str).isin(open_batch_ids)] if not items.empty and "batch_id" in items else items
        if items.empty:
            return [f"open_batch_count: {len(batches)}", "status: NO_OPEN_ITEMS"]
        price_rel = rec.relation("v_daily_price", "daily_price")
        if not price_rel:
            return ["status: NO_PRICE_DATA", f"open_batch_count: {len(batches)}"]
        tracking_rows = []
        item_updates = []
        benchmark_rel = rec.relation("v_index_daily", "index_daily")
        for item in items.to_dict("records"):
            rows, update = _track_item(rec, price_rel, item, cfg.holding_period_days, target_date, benchmark_rel, cfg.benchmark_index)
            tracking_rows.extend(rows)
            if update:
                item_updates.append(update)
        lines = [f"open_batch_count: {len(batches)}", f"tracking_rows: {len(tracking_rows)}", f"item_updates: {len(item_updates)}"]
        if dry_run:
            return lines + ["dry_run: true"]
        rec.write("recommendation_tracking_daily", tracking_rows)
        rec.write("recommendation_item", item_updates)
        _refresh_batch_status(rec, batches, cfg.holding_period_days)
        return lines + ["saved: recommendation_tracking_daily,recommendation_item", f"data_root: {rec.data_root}"]
    finally:
        rec.close()


def _open_batches(rec: RecommendationStore, batch_id: str | None) -> pd.DataFrame:
    where = "WHERE status IN ('WAITING_ENTRY','TRACKING','PARTIAL')"
    params = []
    if batch_id:
        where += " AND batch_id=?"
        params.append(batch_id)
    return rec.query_optional(f"SELECT * FROM v_recommendation_batch {where}", params)


def _track_item(
    rec: RecommendationStore,
    price_rel: str,
    item: dict,
    holding_days: int,
    target_date: str | None,
    benchmark_rel: str | None = None,
    benchmark_index: str = "sh.000300",
) -> tuple[list[dict], dict | None]:
    signal_date = pd.to_datetime(item["signal_date"]).date()
    ts_code = str(item["ts_code"])
    end_clause = f"AND trade_date <= DATE '{target_date}'" if target_date else ""
    prices = rec.query_optional(
        f"""
        SELECT *
        FROM {price_rel}
        WHERE ts_code=? AND trade_date > DATE '{signal_date}' {end_clause}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY trade_date DESC) = 1
        ORDER BY trade_date
        LIMIT {holding_days}
        """,
        [ts_code],
    )
    if prices.empty:
        update = dict(item)
        update["tracking_status"] = "DATA_MISSING"
        update["updated_at"] = pd.Timestamp.now()
        return [], update
    entry = prices.iloc[0]
    entry_date = pd.to_datetime(entry["trade_date"]).date()
    entry_price = _price_value(entry, "open") or _price_value(entry, "close")
    if not entry_price:
        update = dict(item)
        update["tracking_status"] = "DATA_MISSING"
        update["updated_at"] = pd.Timestamp.now()
        return [], update
    benchmark_by_day = _benchmark_returns(rec, benchmark_rel, benchmark_index, signal_date, holding_days, target_date)
    rows = []
    running_max = -10.0
    running_min = 10.0
    max_drawdown = 0.0
    prev_close = entry_price
    for i, row in enumerate(prices.to_dict("records"), start=1):
        close = _price_value(row, "close")
        if not close:
            continue
        cumulative = close / entry_price - 1
        daily_return = close / prev_close - 1 if prev_close else 0.0
        tracking_date = pd.to_datetime(row["trade_date"]).date()
        benchmark_cumulative = benchmark_by_day.get(tracking_date)
        excess_return = cumulative - benchmark_cumulative if benchmark_cumulative is not None else None
        running_max = max(running_max, cumulative)
        running_min = min(running_min, cumulative)
        drawdown = cumulative - running_max
        max_drawdown = min(max_drawdown, drawdown)
        rows.append({
            "batch_id": item["batch_id"],
            "trade_date": tracking_date,
            "signal_date": signal_date,
            "run_mode": item.get("run_mode", "live_shadow"),
            "ts_code": ts_code,
            "tracking_date": tracking_date,
            "trading_day_no": i,
            "entry_date": entry_date,
            "entry_price": float(entry_price),
            "close": float(close),
            "daily_return": float(daily_return),
            "cumulative_return": float(cumulative),
            "net_cumulative_return": float(cumulative),
            "benchmark_cumulative_return": benchmark_cumulative,
            "excess_return": excess_return,
            "industry_benchmark_return": None,
            "industry_excess_return": None,
            "running_max_return": float(running_max),
            "running_min_return": float(running_min),
            "drawdown_from_peak": float(drawdown),
            "max_drawdown_to_date": float(max_drawdown),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
            "data_status": "OK",
            "return_1d": float(cumulative) if i == 1 else None,
            "return_5d": float(cumulative) if i == 5 else None,
            "return_10d": float(cumulative) if i == 10 else None,
            "return_20d": float(cumulative) if i == 20 else None,
            "return_30d": float(cumulative) if i == holding_days else None,
            "created_at": pd.Timestamp.now(),
            "updated_at": pd.Timestamp.now(),
        })
        prev_close = close
    update = dict(item)
    update["entry_date"] = entry_date
    update["entry_price"] = float(entry_price)
    update["tracking_status"] = "COMPLETED" if len(rows) >= holding_days else "TRACKING"
    update["updated_at"] = pd.Timestamp.now()
    return rows, update


def _benchmark_returns(
    rec: RecommendationStore,
    benchmark_rel: str | None,
    benchmark_index: str,
    signal_date,
    holding_days: int,
    target_date: str | None,
) -> dict:
    if not benchmark_rel:
        return {}
    end_clause = f"AND trade_date <= DATE '{target_date}'" if target_date else ""
    prices = rec.query_optional(
        f"""
        SELECT trade_date, close
        FROM {benchmark_rel}
        WHERE index_code=? AND trade_date > DATE '{signal_date}' {end_clause}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY index_code, trade_date ORDER BY trade_date DESC) = 1
        ORDER BY trade_date
        LIMIT {holding_days}
        """,
        [benchmark_index],
    )
    if prices.empty:
        return {}
    entry_close = _price_value(prices.iloc[0], "close")
    if not entry_close:
        return {}
    out = {}
    for row in prices.to_dict("records"):
        close = _price_value(row, "close")
        if close:
            out[pd.to_datetime(row["trade_date"]).date()] = float(close / entry_close - 1)
    return out


def _refresh_batch_status(rec: RecommendationStore, batches: pd.DataFrame, holding_days: int) -> None:
    tracking = rec.read_dataset("recommendation_tracking_daily")
    items = rec.read_dataset("recommendation_item")
    updates = []
    for batch in batches.to_dict("records"):
        bid = batch["batch_id"]
        item_count = len(items[items["batch_id"] == bid])
        complete = tracking[(tracking["batch_id"] == bid) & (tracking["trading_day_no"] >= holding_days)]["ts_code"].nunique() if not tracking.empty else 0
        update = dict(batch)
        if complete >= item_count and item_count:
            update["status"] = "COMPLETED"
        elif complete:
            update["status"] = "PARTIAL"
        else:
            update["status"] = "TRACKING"
        update["trade_date"] = pd.Timestamp(update["signal_date"])
        update["signal_date"] = pd.Timestamp(update["signal_date"])
        update["updated_at"] = pd.Timestamp.now()
        updates.append(update)
    rec.write("recommendation_batch", updates)


def _price_value(row, name: str) -> float | None:
    value = row.get(name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
