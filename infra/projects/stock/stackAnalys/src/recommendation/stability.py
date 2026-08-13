from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import load_recommendation_config
from .stores import RecommendationStore


def evaluate_recommendation_stability(
    db_path: str | Path | None,
    config_path: str | Path | None,
    target_date: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    cfg = load_recommendation_config(config_path)
    rec = RecommendationStore(db_path)
    try:
        rec.refresh()
        items = rec.read_dataset("recommendation_item")
        batches = rec.read_dataset("recommendation_batch")
        if items.empty or batches.empty:
            return ["status: NO_RECOMMENDATION_ITEMS"]
        keep = [col for col in ["batch_id", "recommendation_version", "selector_name", "run_mode", "target_top_n", "fill_ratio"] if col in batches.columns]
        items = items.merge(
            batches[keep].drop_duplicates("batch_id"),
            on="batch_id",
            how="left",
            suffixes=("", "_batch"),
        )
        for col, default in (("run_mode", "live_shadow"), ("selector_name", ""), ("recommendation_version", "")):
            if col not in items:
                items[col] = default
            items[col] = items[col].fillna(default).astype(str)
        rows = []
        detail_rows = []
        for (run_mode, version, selector), group in items.groupby(["run_mode", "recommendation_version", "selector_name"], dropna=False):
            summary, details = _stability_rows(group, str(version), cfg.top_n, target_date, str(selector), str(run_mode))
            rows.extend(summary)
            detail_rows.extend(details)
        lines = [f"stability_rows: {len(rows)}", f"list_change_detail_rows: {len(detail_rows)}"]
        lines.extend(_comparison_lines(rec, rows))
        if dry_run:
            return lines + ["dry_run: true"]
        rec.write("recommendation_stability_daily", rows)
        rec.write("recommendation_list_change_detail", detail_rows)
        return lines + ["saved: recommendation_stability_daily,recommendation_list_change_detail"]
    finally:
        rec.close()


def _stability_rows(items: pd.DataFrame, version: str, top_n: int, target_date: str | None, selector_name: str = "", run_mode: str = "live_shadow") -> tuple[list[dict], list[dict]]:
    rows = []
    details = []
    if target_date:
        items = items[pd.to_datetime(items["signal_date"]) <= pd.to_datetime(target_date)]
    by_date = []
    for signal_date, group in items.groupby("signal_date"):
        target = int(pd.to_numeric(group.get("target_top_n", pd.Series([top_n])), errors="coerce").dropna().iloc[0]) if "target_top_n" in group and not pd.to_numeric(group.get("target_top_n"), errors="coerce").dropna().empty else top_n
        top = group.sort_values("rank").head(target).copy()
        by_date.append((pd.to_datetime(signal_date).date(), top))
    by_date.sort(key=lambda x: x[0])
    consecutive: dict[str, int] = {}
    previous_sets: list[set[str]] = []
    previous_ranks: dict[str, int] = {}
    for idx, (day, top) in enumerate(by_date):
        current = set(top["ts_code"].astype(str))
        current_ranks = dict(zip(top["ts_code"].astype(str), pd.to_numeric(top["rank"], errors="coerce")))
        current_weights = _weights(top)
        prev = previous_sets[-1] if previous_sets else set()
        prev5 = previous_sets[-5] if len(previous_sets) >= 5 else set()
        previous_top = by_date[idx - 1][1] if idx > 0 else pd.DataFrame()
        previous_weights = _weights(previous_top)
        common = current & prev
        new_entries = current - prev
        dropouts = prev - current
        rank_changes = [abs(float(current_ranks[c]) - float(previous_ranks[c])) for c in common if c in previous_ranks and pd.notna(current_ranks[c]) and pd.notna(previous_ranks[c])]
        for code in list(consecutive):
            if code not in current:
                consecutive[code] = 0
        for code in current:
            consecutive[code] = consecutive.get(code, 0) + 1
        consec_values = [consecutive[c] for c in current]
        is_first = idx == 0
        union = current | prev
        target_top_n = int(pd.to_numeric(top.get("target_top_n", pd.Series([top_n])), errors="coerce").dropna().iloc[0]) if "target_top_n" in top and not pd.to_numeric(top.get("target_top_n"), errors="coerce").dropna().empty else int(top_n)
        fill_ratio = len(current) / target_top_n if target_top_n else None
        rows.append({
            "trade_date": day,
            "recommendation_version": version,
            "selector_name": selector_name,
            "run_mode": run_mode,
            "top_n": int(top_n),
            "target_top_n": target_top_n,
            "selected_count": int(len(current)),
            "fill_ratio": fill_ratio,
            "overlap_ratio_1d": None if is_first else (len(common) / len(current) if current else None),
            "overlap_ratio_5d": len(current & prev5) / len(current) if current and prev5 else None,
            "turnover_1d": None if is_first else (len(new_entries) / len(current) if current else None),
            "selected_count_current": int(len(current)),
            "selected_count_previous": int(len(prev)),
            "overlap_count": int(len(common)),
            "overlap_ratio_current": None if is_first else (len(common) / len(current) if current else None),
            "jaccard_overlap": None if is_first else (len(common) / len(union) if union else None),
            "new_entry_count": int(len(new_entries)),
            "new_entry_ratio": None if is_first else (len(new_entries) / len(current) if current else None),
            "dropout_count": int(len(dropouts)),
            "dropout_ratio": None if is_first else (len(dropouts) / len(prev) if prev else None),
            "weight_turnover_1d": None if is_first else _weight_turnover(current_weights, previous_weights),
            "is_first_observation": bool(is_first),
            "mean_rank_change": float(pd.Series(rank_changes).mean()) if rank_changes else 0.0,
            "median_rank_change": float(pd.Series(rank_changes).median()) if rank_changes else 0.0,
            "mean_consecutive_days": float(pd.Series(consec_values).mean()) if consec_values else 0.0,
            "median_consecutive_days": float(pd.Series(consec_values).median()) if consec_values else 0.0,
            "industry_concentration_hhi": _hhi(top.get("industry_name", pd.Series(dtype=str))),
            "created_at": pd.Timestamp.now(),
        })
        details.extend(_detail_rows(day, version, selector_name, run_mode, top, previous_top, current, prev, new_entries, dropouts, common))
        previous_sets.append(current)
        previous_ranks = current_ranks
    return rows, details


def _detail_rows(day, version: str, selector_name: str, run_mode: str, current_top: pd.DataFrame, previous_top: pd.DataFrame, current: set[str], previous: set[str], new_entries: set[str], dropouts: set[str], common: set[str]) -> list[dict]:
    rows = []
    previous_by_code = {str(row.get("ts_code")): row for row in previous_top.to_dict("records")} if not previous_top.empty else {}
    current_by_code = {str(row.get("ts_code")): row for row in current_top.to_dict("records")} if not current_top.empty else {}
    for code in sorted(current | previous):
        row = current_by_code.get(code) or previous_by_code.get(code) or {}
        prev = previous_by_code.get(code, {})
        if code in new_entries:
            change_type = "NEW_ENTRY"
            reason = _entry_reason(row)
        elif code in dropouts:
            change_type = "DROPOUT"
            reason = _dropout_reason(prev)
        elif code in common:
            change_type = "RETAINED"
            reason = "仍在目标名单内"
        else:
            continue
        rows.append({
            "trade_date": day,
            "recommendation_version": version,
            "selector_name": selector_name,
            "run_mode": run_mode,
            "ts_code": code,
            "name": row.get("name", code),
            "change_type": change_type,
            "current_rank": _int_or_none(row.get("rank")),
            "previous_rank": _int_or_none(prev.get("rank")),
            "final_score": _float_or_none(row.get("final_score")),
            "industry_name": row.get("industry_name", ""),
            "change_reason": reason,
            "created_at": pd.Timestamp.now(),
        })
    return rows


def _entry_reason(row: dict) -> str:
    risk = str(row.get("risk_level", "")).upper()
    if risk in {"LOW", "MEDIUM"}:
        return "分数进入阈值或风险恢复"
    return "补足空缺"


def _dropout_reason(row: dict) -> str:
    risk = str(row.get("risk_level", "")).upper()
    if risk == "HIGH":
        return "风险升为 HIGH"
    return "排名跌出保留阈值或被更高分候选替代"


def _int_or_none(value) -> int | None:
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _comparison_lines(rec: RecommendationStore, stability_rows: list[dict]) -> list[str]:
    lines = ["strategy_comparison:"]
    stability = pd.DataFrame(stability_rows)
    item_results = rec.read_dataset("recommendation_item_result")
    batch_results = rec.read_dataset("recommendation_batch_result")
    if item_results.empty and batch_results.empty and stability.empty:
        return lines + ["  no completed results yet"]
    versions = sorted(set(stability.get("recommendation_version", pd.Series(dtype=str)).dropna().astype(str).tolist()) | set(batch_results.get("recommendation_version", pd.Series(dtype=str)).dropna().astype(str).tolist()))
    for version in versions:
        br = batch_results[batch_results["recommendation_version"].astype(str) == version] if not batch_results.empty and "recommendation_version" in batch_results else pd.DataFrame()
        ir = item_results[item_results["batch_id"].isin(br["batch_id"])] if not item_results.empty and not br.empty else pd.DataFrame()
        st = stability[stability["recommendation_version"].astype(str) == version] if not stability.empty else pd.DataFrame()
        lines.append(
            "  "
            + version
            + ": "
            + f"avg_return={_mean(ir, 'gross_return_30d')}, "
            + f"median_return={_median(ir, 'gross_return_30d')}, "
            + f"positive_hit_rate={_mean(ir, 'hit_positive_return')}, "
            + f"benchmark_outperform_rate={_mean(ir, 'outperform_benchmark')}, "
            + f"max_drawdown={_mean(ir, 'max_drawdown_30d')}, "
            + f"turnover={_mean(st, 'weight_turnover_1d')}, "
            + f"transaction_cost={_mean(br, 'transaction_cost')}, "
            + f"overlap_ratio={_mean(st, 'overlap_ratio_current')}, "
            + f"mean_consecutive_days={_mean(st, 'mean_consecutive_days')}, "
            + f"industry_hhi={_mean(st, 'industry_concentration_hhi')}"
        )
    return lines


def _hhi(values: pd.Series) -> float | None:
    clean = values.dropna().astype(str)
    clean = clean[clean != ""]
    if clean.empty:
        return None
    shares = clean.value_counts(normalize=True)
    return float((shares * shares).sum())


def _weights(top: pd.DataFrame) -> dict[str, float]:
    if top.empty:
        return {}
    codes = top["ts_code"].astype(str).tolist()
    if "weight" not in top:
        return {code: 1.0 / len(codes) for code in codes} if codes else {}
    weights = pd.to_numeric(top["weight"], errors="coerce").fillna(0.0)
    total = float(weights.sum())
    if total <= 0:
        return {code: 1.0 / len(codes) for code in codes} if codes else {}
    return dict(zip(codes, (weights / total).tolist()))


def _weight_turnover(current: dict[str, float], previous: dict[str, float]) -> float | None:
    if not previous:
        return None
    keys = set(current) | set(previous)
    return 0.5 * sum(abs(current.get(key, 0.0) - previous.get(key, 0.0)) for key in keys)


def _mean(df: pd.DataFrame, col: str):
    if df.empty or col not in df:
        return None
    clean = pd.to_numeric(df[col], errors="coerce").dropna()
    return round(float(clean.mean()), 6) if not clean.empty else None


def _median(df: pd.DataFrame, col: str):
    if df.empty or col not in df:
        return None
    clean = pd.to_numeric(df[col], errors="coerce").dropna()
    return round(float(clean.median()), 6) if not clean.empty else None
