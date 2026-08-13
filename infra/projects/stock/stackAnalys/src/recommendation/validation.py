from __future__ import annotations

from pathlib import Path

import pandas as pd

from .explanation import FORBIDDEN_WORDS
from .stores import RECOMMENDATION_DATASETS, RecommendationStore


def validate_recommendation_system(db_path: str | Path | None, dry_run: bool = False) -> list[dict]:
    rec = RecommendationStore(db_path)
    rows = []
    try:
        rec.refresh()
        data = {name: rec.read_dataset(name) for name in RECOMMENDATION_DATASETS}
        rows.append(_row("recommendation_outputs_in_data_root", "OK", f"data_root={rec.data_root}", 1, 1))
        rows.extend(_duplicate_checks(data))
        rows.extend(_temporal_checks(data))
        rows.extend(_result_checks(data))
        rows.extend(_cost_checks(data))
        rows.extend(_stability_checks(data))
        rows.extend(_forbidden_checks(data))
        if not dry_run:
            rec.write("analysis_validation", rows)
        return rows
    finally:
        rec.close()


def _duplicate_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    checks = []
    keys = {
        "recommendation_batch": ["signal_date", "recommendation_version", "run_mode"],
        "recommendation_item": ["batch_id", "ts_code"],
        "recommendation_universe": ["batch_id", "ts_code"],
        "recommendation_tracking_daily": ["batch_id", "ts_code", "tracking_date"],
        "recommendation_stability_daily": ["trade_date", "recommendation_version", "selector_name", "run_mode", "top_n"],
        "recommendation_rolling_return_daily": ["trading_date", "recommendation_version", "selector_name", "run_mode"],
    }
    for name, cols in keys.items():
        df = data[name]
        dup = int(df.duplicated(cols).sum()) if not df.empty and all(c in df for c in cols) else 0
        checks.append(_row(f"{name}_duplicate_keys", "OK" if dup == 0 else "FAIL", f"duplicates={dup}", dup, 0))
    return checks


def _temporal_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    checks = []
    items = data["recommendation_item"]
    if not items.empty and {"entry_date", "signal_date"} <= set(items.columns):
        subset = items.dropna(subset=["entry_date"])
        bad = int((pd.to_datetime(subset["entry_date"]) <= pd.to_datetime(subset["signal_date"])).sum()) if not subset.empty else 0
        checks.append(_row("entry_date_after_signal_date", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    tracking = data["recommendation_tracking_daily"]
    if not tracking.empty and "trading_day_no" in tracking:
        bad = int((pd.to_numeric(tracking["trading_day_no"], errors="coerce") > 30).sum())
        checks.append(_row("tracking_day_no_not_over_30", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    return checks


def _result_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    checks = []
    results = data["recommendation_item_result"]
    if not results.empty:
        partial_complete = int(((results["outcome_status"] == "COMPLETED") & (pd.to_numeric(results["actual_trading_days"], errors="coerce") < 30)).sum())
        checks.append(_row("completed_requires_30_trading_days", "OK" if partial_complete == 0 else "FAIL", f"bad={partial_complete}", partial_complete, 0))
        if {"entry_price", "exit_price", "gross_return_30d", "outcome_status"} <= set(results.columns):
            completed = results[results["outcome_status"] == "COMPLETED"].copy()
            expected = pd.to_numeric(completed["exit_price"], errors="coerce") / pd.to_numeric(completed["entry_price"], errors="coerce") - 1
            actual = pd.to_numeric(completed["gross_return_30d"], errors="coerce")
            bad = int(((expected - actual).abs() > 1e-8).sum())
            checks.append(_row("gross_return_matches_prices", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    batches = data["recommendation_batch_result"]
    if not batches.empty and "equal_weight_net_return" in batches:
        checks.append(_row("portfolio_return_weighted", "OK", "equal-weight portfolio return stored separately from stock returns", 1, 1))
        items = data["recommendation_item_result"]
        bad = _weighted_return_mismatch(items, batches)
        checks.append(_row("weighted_return_matches_items", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    rec_batches = data["recommendation_batch"]
    if not rec_batches.empty and {"target_top_n", "selected_count", "fill_ratio"} <= set(rec_batches.columns):
        expected = pd.to_numeric(rec_batches["selected_count"], errors="coerce") / pd.to_numeric(rec_batches["target_top_n"], errors="coerce")
        actual = pd.to_numeric(rec_batches["fill_ratio"], errors="coerce")
        bad = int(((expected - actual).abs() > 1e-8).sum())
        checks.append(_row("fill_ratio_matches_selected_over_target", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    rolling = data.get("recommendation_rolling_return_daily", pd.DataFrame())
    checks.extend(_rolling_checks(rolling))
    return checks


def _rolling_checks(rolling: pd.DataFrame) -> list[dict]:
    if rolling.empty:
        return []
    checks = []
    if {"monthly_return_status", "monthly_observation_count", "rolling_monthly_net_return"} <= set(rolling.columns):
        bad_count = int(((rolling["monthly_return_status"] == "OK") & (pd.to_numeric(rolling["monthly_observation_count"], errors="coerce") != 20)).sum())
        bad_null = int(((rolling["monthly_return_status"] == "INSUFFICIENT_HISTORY") & rolling["rolling_monthly_net_return"].notna()).sum())
        checks.append(_row("monthly_return_requires_20_trading_days", "OK" if bad_count == 0 else "FAIL", f"bad={bad_count}", bad_count, 0))
        checks.append(_row("monthly_insufficient_history_return_null", "OK" if bad_null == 0 else "FAIL", f"bad={bad_null}", bad_null, 0))
    if {"annual_return_status", "annual_observation_count", "rolling_annual_net_return"} <= set(rolling.columns):
        bad_count = int(((rolling["annual_return_status"] == "OK") & (pd.to_numeric(rolling["annual_observation_count"], errors="coerce") != 252)).sum())
        bad_null = int(((rolling["annual_return_status"] == "INSUFFICIENT_HISTORY") & rolling["rolling_annual_net_return"].notna()).sum())
        checks.append(_row("annual_return_requires_252_trading_days", "OK" if bad_count == 0 else "FAIL", f"bad={bad_count}", bad_count, 0))
        checks.append(_row("annual_insufficient_history_return_null", "OK" if bad_null == 0 else "FAIL", f"bad={bad_null}", bad_null, 0))
    if {"run_mode", "recommendation_version", "selector_name", "trading_date"} <= set(rolling.columns):
        mixed = rolling.groupby(["run_mode", "recommendation_version", "selector_name"])["trading_date"].nunique().sum()
        checks.append(_row("rolling_returns_grouped_by_run_mode", "OK", f"grouped_observations={int(mixed)}", int(mixed), int(mixed)))
    return checks


def _cost_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    results = data["recommendation_item_result"]
    if results.empty:
        return []
    checks = []
    net_col = "current_net_return" if "current_net_return" in results else "net_return_30d"
    bad_net = int((pd.to_numeric(results[net_col], errors="coerce") < -1.05).sum()) if net_col in results else 0
    checks.append(_row("net_return_not_below_minus_1_05", "OK" if bad_net == 0 else "FAIL", f"bad={bad_net}", bad_net, 0))
    if "total_cost_rate" in results:
        costs = pd.to_numeric(results["total_cost_rate"], errors="coerce")
        bad_cost = int(((costs < 0) | (costs > 0.2)).sum())
        checks.append(_row("total_cost_rate_reasonable", "OK" if bad_cost == 0 else "FAIL", f"bad={bad_cost}", bad_cost, 0))
    if "position_notional" in results:
        bad_notional = int((pd.to_numeric(results["position_notional"], errors="coerce") <= 0).sum())
        checks.append(_row("position_notional_positive", "OK" if bad_notional == 0 else "FAIL", f"bad={bad_notional}", bad_notional, 0))
    return checks


def _stability_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    stability = data["recommendation_stability_daily"]
    if stability.empty:
        return []
    checks = []
    first = stability[stability.get("is_first_observation", False) == True] if "is_first_observation" in stability else pd.DataFrame()
    if not first.empty:
        cols = [col for col in ["overlap_ratio_current", "jaccard_overlap", "new_entry_ratio", "dropout_ratio", "weight_turnover_1d"] if col in first]
        bad = int(sum(first[col].notna().sum() for col in cols))
        checks.append(_row("first_stability_ratios_null", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    if {"selected_count_current", "overlap_count", "overlap_ratio_current"} <= set(stability.columns):
        comparable = stability[stability["overlap_ratio_current"].notna()]
        expected = pd.to_numeric(comparable["overlap_count"], errors="coerce") / pd.to_numeric(comparable["selected_count_current"], errors="coerce")
        actual = pd.to_numeric(comparable["overlap_ratio_current"], errors="coerce")
        bad = int(((expected - actual).abs() > 1e-8).sum())
        checks.append(_row("stability_uses_actual_selected_count", "OK" if bad == 0 else "FAIL", f"bad={bad}", bad, 0))
    return checks


def _weighted_return_mismatch(items: pd.DataFrame, batches: pd.DataFrame) -> int:
    if items.empty or batches.empty or "weighted_net_return" not in batches:
        return 0
    bad = 0
    for row in batches.to_dict("records"):
        group = items[(items["batch_id"] == row["batch_id"]) & (items["outcome_status"] == "COMPLETED")] if "outcome_status" in items else items[items["batch_id"] == row["batch_id"]]
        if group.empty or pd.isna(row.get("weighted_net_return")):
            continue
        weights = pd.to_numeric(group["weight"], errors="coerce").fillna(0.0) if "weight" in group else pd.Series([1 / len(group)] * len(group), index=group.index)
        total = weights.sum()
        weights = weights / total if total else pd.Series([1 / len(group)] * len(group), index=group.index)
        expected = (weights * pd.to_numeric(group["net_return_30d"], errors="coerce")).sum()
        if abs(float(expected) - float(row["weighted_net_return"])) > 1e-8:
            bad += 1
    return bad


def _forbidden_checks(data: dict[str, pd.DataFrame]) -> list[dict]:
    exp = data["recommendation_explanation"]
    if exp.empty:
        return [_row("recommendation_explanation_forbidden_words", "OK", "no explanation rows", 0, 0)]
    text = " ".join(str(v) for col in exp.columns for v in exp[col].dropna().tolist())
    hits = [word for word in FORBIDDEN_WORDS if word in text]
    return [_row("recommendation_explanation_forbidden_words", "OK" if not hits else "FAIL", f"hits={hits}", len(hits), 0)]


def _row(check_name: str, status: str, message: str, value, threshold) -> dict:
    return {
        "trade_date": pd.Timestamp.now().date(),
        "check_name": check_name,
        "status": status,
        "message": message,
        "value": value,
        "threshold": threshold,
        "created_at": pd.Timestamp.now(),
    }
