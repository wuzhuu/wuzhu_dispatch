from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from .config import load_recommendation_config
from .dates import trade_date
from .performance import calculate_rolling_returns
from .stores import RecommendationStore


def finalize_results(
    db_path: str | Path | None,
    config_path: str | Path | None,
    batch_id: str | None = None,
    dry_run: bool = False,
    recompute_costs: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    cfg = load_recommendation_config(config_path)
    rec = RecommendationStore(db_path)
    try:
        rec.refresh()
        tracking = rec.read_dataset("recommendation_tracking_daily")
        items = rec.read_dataset("recommendation_item")
        batches = rec.read_dataset("recommendation_batch")
        if batch_id:
            tracking = tracking[tracking["batch_id"] == batch_id]
            items = items[items["batch_id"] == batch_id]
            batches = batches[batches["batch_id"] == batch_id]
        if start:
            tracking = tracking[pd.to_datetime(tracking["signal_date"]) >= pd.to_datetime(start)]
            items = items[pd.to_datetime(items["signal_date"]) >= pd.to_datetime(start)]
            batches = batches[pd.to_datetime(batches["signal_date"]) >= pd.to_datetime(start)]
        if end:
            tracking = tracking[pd.to_datetime(tracking["signal_date"]) <= pd.to_datetime(end)]
            items = items[pd.to_datetime(items["signal_date"]) <= pd.to_datetime(end)]
            batches = batches[pd.to_datetime(batches["signal_date"]) <= pd.to_datetime(end)]
        if tracking.empty or items.empty:
            return ["status: NO_TRACKING_DATA"]
        item_rows = _item_results(tracking, items, cfg.holding_period_days, cfg)
        batch_rows = _batch_results(item_rows, batches)
        live_rows = _live_portfolio(tracking, batches)
        rolling_rows = calculate_rolling_returns(pd.DataFrame(live_rows), min_annualized_days=60)
        repair_rows = _repair_rows(item_rows, batch_rows, recompute_costs)
        bad_net = sum(1 for row in item_rows if row.get("current_net_return") is not None and row["current_net_return"] < -1.05)
        lines = [
            f"item_results: {len(item_rows)}",
            f"batch_results: {len(batch_rows)}",
            f"live_portfolio_days: {len(live_rows)}",
            f"rolling_return_rows: {len(rolling_rows)}",
            f"recompute_costs: {recompute_costs}",
            f"net_return_below_minus_1_05: {bad_net}",
        ]
        if dry_run:
            return lines + ["dry_run: true"]
        rec.write("recommendation_item_result", item_rows)
        rec.write("recommendation_batch_result", batch_rows)
        rec.write("recommendation_live_portfolio_daily", live_rows)
        rec.write("recommendation_rolling_return_daily", rolling_rows)
        rec.write("recommendation_repair_log", repair_rows)
        return lines + ["saved: recommendation_item_result,recommendation_batch_result,recommendation_live_portfolio_daily,recommendation_rolling_return_daily"]
    finally:
        rec.close()


def evaluate_system(db_path: str | Path | None, config_path: str | Path | None, target_date: str | None = None, dry_run: bool = False) -> list[str]:
    cfg = load_recommendation_config(config_path)
    rec = RecommendationStore(db_path)
    try:
        rec.refresh()
        items = rec.read_dataset("recommendation_item_result")
        batches = rec.read_dataset("recommendation_batch_result")
        items = items[items.get("outcome_status") == "COMPLETED"] if not items.empty and "outcome_status" in items else items
        if items.empty:
            return ["status: NO_COMPLETED_RESULTS"]
        rows = [{
            "evaluation_date": pd.to_datetime(target_date).date() if target_date else pd.Timestamp.now().date(),
            "trade_date": pd.to_datetime(target_date).date() if target_date else pd.Timestamp.now().date(),
            "recommendation_version": cfg.recommendation_version,
            "completed_batch_count": int(batches["batch_id"].nunique()) if not batches.empty else 0,
            "completed_stock_count": int(len(items)),
            "average_return_30d": float(items["gross_return_30d"].mean()),
            "median_return_30d": float(items["gross_return_30d"].median()),
            "weighted_return_30d": _numeric_mean(batches["weighted_net_return"]) if not batches.empty and "weighted_net_return" in batches else float(items["net_return_30d"].mean()),
            "positive_hit_rate": float(items["hit_positive_return"].mean()),
            "benchmark_outperform_rate": _bool_mean(items["outperform_benchmark"]) if "outperform_benchmark" in items else None,
            "average_excess_return": _numeric_mean(items["excess_return_30d"]) if "excess_return_30d" in items else None,
            "average_max_drawdown": float(items["max_drawdown_30d"].mean()),
            "profit_loss_ratio": _profit_loss_ratio(items["gross_return_30d"]),
            "return_std": float(items["gross_return_30d"].std()) if len(items) > 1 else 0.0,
            "information_ratio": None,
            "created_at": pd.Timestamp.now(),
        }]
        if dry_run:
            return [f"evaluation_rows: {len(rows)}", "dry_run: true"]
        rec.write("recommendation_evaluation_daily", rows)
        return [f"evaluation_rows: {len(rows)}", "saved: recommendation_evaluation_daily"]
    finally:
        rec.close()


def _item_results(tracking: pd.DataFrame, items: pd.DataFrame, holding_days: int, cfg) -> list[dict]:
    rows = []
    for (batch_id, ts_code), hist in tracking.groupby(["batch_id", "ts_code"]):
        hist = hist.sort_values("trading_day_no")
        item = items[(items["batch_id"] == batch_id) & (items["ts_code"] == ts_code)].head(1)
        item_row = item.iloc[0].to_dict() if not item.empty else {}
        last = hist.iloc[-1]
        completed = int(last["trading_day_no"]) >= holding_days
        gross = float(last["cumulative_return"])
        weight = _float_or_default(item_row.get("weight"), None)
        selected_count = len(items[items["batch_id"] == batch_id]) if "batch_id" in items else None
        cost = _net_return(gross, cfg, weight=weight, selected_count=selected_count)
        net = cost["net_return"]
        completed = int(last["trading_day_no"]) >= holding_days
        final_gross = gross if completed else None
        final_net = net if completed else None
        rows.append({
            "batch_id": batch_id,
            "trade_date": trade_date(item_row.get("signal_date") or last["signal_date"]),
            "signal_date": trade_date(item_row.get("signal_date") or last["signal_date"]),
            "ts_code": ts_code,
            "name": item_row.get("name", ts_code),
            "entry_date": trade_date(last["entry_date"]),
            "entry_price": float(last["entry_price"]),
            "exit_date": trade_date(last["tracking_date"]),
            "exit_price": float(last["close"]),
            "actual_trading_days": int(last["trading_day_no"]),
            "gross_return_30d": final_gross,
            "net_return_30d": final_net,
            "current_gross_return": gross,
            "current_net_return": net,
            "final_gross_return_30d": final_gross,
            "final_net_return_30d": final_net,
            "benchmark_return_30d": last.get("benchmark_cumulative_return"),
            "excess_return_30d": last.get("excess_return"),
            "industry_return_30d": last.get("industry_benchmark_return"),
            "industry_excess_return_30d": last.get("industry_excess_return"),
            "max_gain_30d": float(hist["cumulative_return"].max()),
            "max_drawdown_30d": float(hist["max_drawdown_to_date"].min()),
            "positive_day_count": int((hist["daily_return"] > 0).sum()),
            "negative_day_count": int((hist["daily_return"] < 0).sum()),
            "first_positive_day": int(hist[hist["cumulative_return"] > 0]["trading_day_no"].min()) if (hist["cumulative_return"] > 0).any() else None,
            "hit_positive_return": bool(final_gross is not None and final_gross > 0),
            "weight": weight,
            "position_notional": cost["position_notional"],
            "buy_commission": cost["buy_commission"],
            "sell_commission": cost["sell_commission"],
            "buy_cost_rate": cost["buy_cost_rate"],
            "sell_cost_rate": cost["sell_cost_rate"],
            "stamp_duty_cost_rate": cost["stamp_duty_cost_rate"],
            "slippage_cost_rate": cost["slippage_cost_rate"],
            "total_cost_rate": cost["total_cost_rate"],
            "cost_warning": cost["cost_warning"],
            "outperform_benchmark": _outperform(net, last.get("benchmark_cumulative_return")) if completed else None,
            "outcome_status": "COMPLETED" if completed else "PARTIAL",
            "completed_at": pd.Timestamp.now(),
            "created_at": pd.Timestamp.now(),
        })
    return rows


def _batch_results(item_rows: list[dict], batches: pd.DataFrame) -> list[dict]:
    if not item_rows:
        return []
    items = pd.DataFrame(item_rows)
    rows = []
    for batch_id, group in items.groupby("batch_id"):
        batch = batches[batches["batch_id"] == batch_id].head(1)
        version = batch["recommendation_version"].iloc[0] if not batch.empty and "recommendation_version" in batch else ""
        selector_name = batch["selector_name"].iloc[0] if not batch.empty and "selector_name" in batch else ""
        completed = group[group["outcome_status"] == "COMPLETED"].copy() if "outcome_status" in group else group.copy()
        weights = _normalized_weights(completed)
        gross = pd.to_numeric(completed.get("gross_return_30d"), errors="coerce") if not completed.empty else pd.Series(dtype=float)
        returns = pd.to_numeric(completed.get("net_return_30d"), errors="coerce") if not completed.empty else pd.Series(dtype=float)
        benchmark = pd.to_numeric(completed["benchmark_return_30d"], errors="coerce") if not completed.empty and "benchmark_return_30d" in completed else pd.Series(dtype=float)
        weighted_gross = _weighted_sum(gross, weights)
        weighted_net = _weighted_sum(returns, weights)
        weighted_benchmark = _weighted_sum(benchmark, weights)
        weighted_excess = weighted_net - weighted_benchmark if weighted_net is not None and weighted_benchmark is not None else None
        rows.append({
            "batch_id": batch_id,
            "trade_date": trade_date(group["signal_date"].iloc[0]),
            "signal_date": trade_date(group["signal_date"].iloc[0]),
            "recommendation_version": version,
            "selector_name": selector_name,
            "selected_count": int(len(group)),
            "completed_count": int(len(completed)),
            "positive_count": int((gross > 0).sum()),
            "negative_count": int((gross < 0).sum()),
            "hit_rate": float((gross > 0).mean()) if len(gross.dropna()) else None,
            "equal_weight_gross_return": weighted_gross,
            "equal_weight_net_return": weighted_net,
            "weighted_gross_return": weighted_gross,
            "weighted_net_return": weighted_net,
            "simple_mean_gross_return": float(gross.mean()) if len(gross.dropna()) else None,
            "simple_mean_net_return": float(returns.mean()) if len(returns.dropna()) else None,
            "weighting_method": "equal" if completed.empty or "weight" not in completed else "configured",
            "weight_sum": float(pd.to_numeric(completed.get("weight", pd.Series(dtype=float)), errors="coerce").sum()) if not completed.empty else 0.0,
            "benchmark_return": weighted_benchmark,
            "excess_return": weighted_excess,
            "industry_adjusted_return": None,
            "median_stock_return": float(returns.median()) if len(returns.dropna()) else None,
            "best_stock_return": float(returns.max()) if len(returns.dropna()) else None,
            "worst_stock_return": float(returns.min()) if len(returns.dropna()) else None,
            "portfolio_max_drawdown": float(pd.to_numeric(completed.get("max_drawdown_30d"), errors="coerce").min()) if not completed.empty else None,
            "annualized_volatility": float(returns.std() * (252 ** 0.5)) if len(returns) > 1 else 0.0,
            "sharpe": None,
            "concentration_hhi": 1.0 / len(group) if len(group) else None,
            "transaction_cost": float(pd.to_numeric(completed.get("total_cost_rate"), errors="coerce").mean()) if not completed.empty and "total_cost_rate" in completed else None,
            "completed_at": pd.Timestamp.now(),
            "created_at": pd.Timestamp.now(),
        })
    return rows


def _live_portfolio(tracking: pd.DataFrame, batches: pd.DataFrame | None = None) -> list[dict]:
    if tracking.empty:
        return []
    tracking = tracking.copy()
    if batches is not None and not batches.empty:
        keep = [col for col in ["batch_id", "recommendation_version", "selector_name", "run_mode"] if col in batches.columns]
        if keep:
            tracking = tracking.merge(batches[keep].drop_duplicates("batch_id"), on="batch_id", how="left", suffixes=("", "_batch"))
    if "run_mode" not in tracking:
        tracking["run_mode"] = "live_shadow"
    tracking["run_mode"] = tracking["run_mode"].fillna("live_shadow").astype(str)
    if "recommendation_version" not in tracking:
        tracking["recommendation_version"] = ""
    if "selector_name" not in tracking:
        tracking["selector_name"] = ""
    rows = []
    group_cols = ["run_mode", "recommendation_version", "selector_name"]
    for keys, scope in tracking.groupby(group_cols, dropna=False):
        run_mode, version, selector = keys
        gross_equity = 1.0
        net_equity = 1.0
        for day, group in scope.sort_values("tracking_date").groupby("tracking_date"):
            gross_daily = pd.to_numeric(group["daily_return"], errors="coerce").mean()
            net_daily = pd.to_numeric(group.get("net_daily_return", group["daily_return"]), errors="coerce").mean()
            gross_equity *= 1 + float(gross_daily)
            net_equity *= 1 + float(net_daily)
            benchmark_cumulative = _numeric_mean(group["benchmark_cumulative_return"]) if "benchmark_cumulative_return" in group else None
            excess_cumulative = (net_equity - 1) - benchmark_cumulative if benchmark_cumulative is not None else None
            rows.append({
                "trading_date": pd.to_datetime(day).date(),
                "trade_date": pd.to_datetime(day).date(),
                "run_mode": str(run_mode or "live_shadow"),
                "recommendation_version": str(version or ""),
                "selector_name": str(selector or ""),
                "active_batch_count": int(group["batch_id"].nunique()),
                "active_position_count": int(len(group)),
                "gross_daily_return": float(gross_daily),
                "net_daily_return": float(net_daily),
                "gross_equity": float(gross_equity),
                "net_equity": float(net_equity),
                "cumulative_return": float(net_equity - 1),
                "benchmark_cumulative_return": benchmark_cumulative,
                "excess_cumulative_return": excess_cumulative,
                "turnover": 0.0,
                "transaction_cost": 0.0,
                "created_at": pd.Timestamp.now(),
            })
    return rows


def _net_return(gross: float, cfg, weight: float | None = None, selected_count: int | None = None) -> dict[str, float | str]:
    gross_d = _dec(gross)
    notional = _position_notional(cfg, weight, selected_count)
    commission_rate = _dec(cfg.commission_rate)
    min_commission = _dec(cfg.minimum_commission)
    slippage = _dec(cfg.slippage_rate)
    stamp = _dec(cfg.sell_stamp_duty_rate)
    buy_commission = max(notional * commission_rate, min_commission)
    sell_notional = max(notional * (Decimal("1") + gross_d), Decimal("0"))
    sell_commission = max(sell_notional * commission_rate, min_commission)
    buy_cost_rate = buy_commission / notional
    sell_cost_rate = sell_commission / notional
    slippage_cost_rate = slippage * Decimal("2")
    total_cost_rate = buy_cost_rate + sell_cost_rate + stamp + slippage_cost_rate
    net = gross_d - total_cost_rate
    warning = "NET_RETURN_BELOW_-1.05" if net < Decimal("-1.05") else ""
    return {
        "net_return": _float(net),
        "position_notional": _float(notional),
        "buy_commission": _float(buy_commission),
        "sell_commission": _float(sell_commission),
        "buy_cost_rate": _float(buy_cost_rate),
        "sell_cost_rate": _float(sell_cost_rate),
        "stamp_duty_cost_rate": _float(stamp),
        "slippage_cost_rate": _float(slippage_cost_rate),
        "total_cost_rate": _float(total_cost_rate),
        "cost_warning": warning,
    }


def _position_notional(cfg, weight: float | None, selected_count: int | None) -> Decimal:
    configured = _dec(getattr(cfg, "position_notional", 0))
    if configured > 0:
        return configured
    batch_capital = _dec(getattr(cfg, "batch_capital", 0))
    if batch_capital > 0 and weight is not None and weight > 0:
        return batch_capital * _dec(weight)
    if batch_capital > 0 and selected_count:
        return batch_capital / _dec(selected_count)
    return Decimal("10000")


def _normalized_weights(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    if "weight" not in df:
        return pd.Series([1.0 / len(df)] * len(df), index=df.index)
    weights = pd.to_numeric(df["weight"], errors="coerce").fillna(0.0)
    total = float(weights.sum())
    if total <= 0:
        return pd.Series([1.0 / len(df)] * len(df), index=df.index)
    return weights / total


def _weighted_sum(values: pd.Series, weights: pd.Series) -> float | None:
    aligned = pd.concat([values, weights], axis=1).dropna()
    if aligned.empty:
        return None
    return float((aligned.iloc[:, 0] * aligned.iloc[:, 1]).sum())


def _outperform(net_return, benchmark_return) -> bool | None:
    try:
        net = float(net_return)
        benchmark = float(benchmark_return)
    except (TypeError, ValueError):
        return None
    if pd.isna(net) or pd.isna(benchmark):
        return None
    return net > benchmark


def _dec(value) -> Decimal:
    return Decimal(str(0 if value is None or pd.isna(value) else value))


def _float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP))


def _float_or_default(value, default):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _repair_rows(item_rows: list[dict], batch_rows: list[dict], recompute_costs: bool) -> list[dict]:
    if not recompute_costs:
        return []
    return [
        {
            "trade_date": pd.Timestamp.now().date(),
            "check_name": "recompute_recommendation_costs",
            "status": "OK",
            "message": "recomputed recommendation derivative net returns and weighted batch results",
            "affected_rows": len(item_rows) + len(batch_rows),
            "created_at": pd.Timestamp.now(),
        }
    ]


def _profit_loss_ratio(series: pd.Series) -> float | None:
    wins = series[series > 0]
    losses = series[series < 0]
    if losses.empty:
        return None
    return float(wins.mean() / abs(losses.mean())) if not wins.empty else 0.0


def _bool_mean(series: pd.Series) -> float | None:
    clean = series.dropna()
    if clean.empty:
        return None
    return float(clean.astype(bool).mean())


def _numeric_mean(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.mean())
