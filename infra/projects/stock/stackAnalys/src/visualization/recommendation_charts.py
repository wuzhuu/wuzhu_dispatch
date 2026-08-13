from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.path_resolver import resolve_chart_root


def build_recommendation_charts(db_path: str | Path | None = None, target_date: str | None = None) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    # 配置中文字体
    font_path = Path.home() / ".fonts" / "NotoSansCJKsc-Regular.otf"
    if font_path.exists():
        fm.fontManager.addfont(str(font_path))
        plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "SimHei", "DejaVu Sans"]
    else:
        plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    store = AnalysisLakeStore(db_path)
    chart_root = resolve_chart_root(store.db_path) / "recommendation"
    chart_root.mkdir(parents=True, exist_ok=True)
    trade_date = str(pd.to_datetime(target_date).date()) if target_date else str(pd.Timestamp.now().date())
    snapshots: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        store.refresh_views([
            "recommendation_batch_result",
            "recommendation_item_result",
            "recommendation_live_portfolio_daily",
            "recommendation_rolling_return_daily",
            "recommendation_evaluation_daily",
            "recommendation_stability_daily",
            "visualization_snapshot",
            "visualization_metric",
        ])
        builders = [
            _batch_return_chart,
            _item_return_distribution,
            _partial_return_distribution,
            _live_portfolio_chart,
            _rolling_monthly_return_chart,
            _rolling_annual_return_chart,
            _list_change_summary_chart,
            _evaluation_hit_rate_chart,
            _stability_overlap_chart,
            _stability_turnover_chart,
            _stability_entry_dropout_chart,
            _consecutive_days_distribution,
            _strategy_return_comparison,
        ]
        for builder in builders:
            try:
                result = builder(store, chart_root, trade_date, plt)
                snapshots.append(result["snapshot"])
                metrics.extend(result.get("metrics", []))
                print(f"chart: {result['snapshot']['chart_type']} -> {result['snapshot']['image_path'] or 'WARNING'}")
            except Exception as exc:
                warning = f"{builder.__name__}: {exc!r}"
                warnings.append(warning)
                snapshots.append(_warning(trade_date, builder.__name__, "Recommendation Chart", warning))
                print(f"WARNING {warning}")
        if snapshots:
            store.write_dataset("visualization_snapshot", pd.DataFrame(snapshots))
        if metrics:
            store.write_dataset("visualization_metric", pd.DataFrame(metrics))
        return {"snapshots": snapshots, "metrics": metrics, "warnings": warnings, "chart_root": str(chart_root)}
    finally:
        store.close()


def _batch_return_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_batch_result")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_batch_return", "Recommendation Batch Return", "no batch result data"), "metrics": []}
    df = df[df.get("completed_count", 0) > 0].sort_values("signal_date").tail(60)
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_batch_return", "Recommendation Batch Return", "no completed batch result data"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    return_col = "weighted_net_return" if "weighted_net_return" in df else "equal_weight_net_return"
    ax.bar(pd.to_datetime(df["signal_date"]).dt.strftime("%Y-%m-%d"), pd.to_numeric(df[return_col], errors="coerce"))
    ax.set_title("Recommendation Batch 30D Net Return")
    ax.tick_params(axis="x", rotation=60)
    path = _save(fig, chart_root, trade_date, "recommendation_batch_return", plt)
    return _result(trade_date, "recommendation_batch_return", "Recommendation Batch 30D Net Return", path, {
        "batch_count": len(df),
        "latest_net_return": df[return_col].iloc[-1],
    })


def _item_return_distribution(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_item_result")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_item_return_distribution", "Recommendation Item Return Distribution", "no item result data"), "metrics": []}
    if "outcome_status" in df:
        df = df[df["outcome_status"] == "COMPLETED"]
    returns = pd.to_numeric(df["net_return_30d"], errors="coerce").dropna()
    if returns.empty:
        return {"snapshot": _warning(trade_date, "recommendation_item_return_distribution", "Recommendation Item Return Distribution", "no numeric item returns"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(returns, bins=min(20, max(5, len(returns))))
    ax.set_title("Recommendation Item 30D Net Return Distribution")
    path = _save(fig, chart_root, trade_date, "recommendation_item_return_distribution", plt)
    return _result(trade_date, "recommendation_item_return_distribution", "Recommendation Item 30D Net Return Distribution", path, {
        "stock_count": len(returns),
        "positive_ratio": float((returns > 0).mean()),
        "median_return": float(returns.median()),
    })


def _partial_return_distribution(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_item_result")
    if df.empty or "outcome_status" not in df:
        return {"snapshot": _warning(trade_date, "recommendation_partial_current_return", "Recommendation Partial Current Return", "no partial item data"), "metrics": []}
    df = df[df["outcome_status"] == "PARTIAL"]
    returns = pd.to_numeric(df.get("current_net_return"), errors="coerce").dropna()
    if returns.empty:
        return {"snapshot": _warning(trade_date, "recommendation_partial_current_return", "Recommendation Partial Current Return", "no partial current returns"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(returns, bins=min(20, max(5, len(returns))))
    ax.set_title("Partial Current Net Return Distribution")
    path = _save(fig, chart_root, trade_date, "recommendation_partial_current_return", plt)
    return _result(trade_date, "recommendation_partial_current_return", "Recommendation Partial Current Return", path, {"partial_count": len(returns), "median_current_return": float(returns.median())})


def _live_portfolio_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_live_portfolio_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_live_portfolio", "Recommendation Live Portfolio", "no live portfolio data"), "metrics": []}
    df = df.sort_values("trading_date").tail(180)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(pd.to_datetime(df["trading_date"]), pd.to_numeric(df["cumulative_return"], errors="coerce"), label="cumulative_return")
    ax.set_title("Recommendation Rolling Shadow Portfolio")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "recommendation_live_portfolio", plt)
    return _result(trade_date, "recommendation_live_portfolio", "Recommendation Rolling Shadow Portfolio", path, {
        "active_batch_count": df["active_batch_count"].iloc[-1],
        "latest_cumulative_return": df["cumulative_return"].iloc[-1],
    })


def _rolling_monthly_return_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_rolling_return_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "rolling_monthly_return", "Rolling Monthly Return", "no rolling return data"), "metrics": []}
    df = df[df.get("monthly_return_status", "") == "OK"].sort_values("trading_date").tail(180)
    if df.empty:
        return {"snapshot": _warning(trade_date, "rolling_monthly_return", "Rolling Monthly Return", "INSUFFICIENT_HISTORY"), "metrics": []}
    latest = df.iloc[-1]
    fig, ax = plt.subplots(figsize=(10, 5))
    x = pd.to_datetime(df["trading_date"])
    ax.plot(x, pd.to_numeric(df["rolling_monthly_net_return"], errors="coerce"), label="portfolio_net")
    if "rolling_monthly_benchmark_return" in df:
        ax.plot(x, pd.to_numeric(df["rolling_monthly_benchmark_return"], errors="coerce"), label="benchmark")
    if "rolling_monthly_excess_return" in df:
        ax.plot(x, pd.to_numeric(df["rolling_monthly_excess_return"], errors="coerce"), label="excess")
    ax.set_title(f"Rolling Monthly Return ({latest.get('run_mode')}, {latest.get('selector_name')}, obs={latest.get('monthly_observation_count')})")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "rolling_monthly_return", plt)
    return _result(trade_date, "rolling_monthly_return", "Rolling Monthly Return", path, {
        "latest_rolling_monthly_net_return": latest.get("rolling_monthly_net_return"),
        "latest_monthly_excess_return": latest.get("rolling_monthly_excess_return"),
        "monthly_observation_count": latest.get("monthly_observation_count"),
    })


def _rolling_annual_return_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_rolling_return_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "rolling_annual_return", "Rolling Annual Return", "no rolling return data"), "metrics": []}
    df = df[df.get("annual_return_status", "") == "OK"].sort_values("trading_date").tail(500)
    if df.empty:
        return {"snapshot": _warning(trade_date, "rolling_annual_return", "Rolling Annual Return", "INSUFFICIENT_HISTORY"), "metrics": []}
    latest = df.iloc[-1]
    fig, ax = plt.subplots(figsize=(10, 5))
    x = pd.to_datetime(df["trading_date"])
    ax.plot(x, pd.to_numeric(df["rolling_annual_net_return"], errors="coerce"), label="portfolio_net")
    if "rolling_annual_benchmark_return" in df:
        ax.plot(x, pd.to_numeric(df["rolling_annual_benchmark_return"], errors="coerce"), label="benchmark")
    if "rolling_annual_excess_return" in df:
        ax.plot(x, pd.to_numeric(df["rolling_annual_excess_return"], errors="coerce"), label="excess")
    ax.set_title(f"Rolling Annual Return ({latest.get('run_mode')}, {latest.get('selector_name')}, obs={latest.get('annual_observation_count')})")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "rolling_annual_return", plt)
    return _result(trade_date, "rolling_annual_return", "Rolling Annual Return", path, {
        "latest_rolling_annual_net_return": latest.get("rolling_annual_net_return"),
        "latest_annual_excess_return": latest.get("rolling_annual_excess_return"),
        "annual_observation_count": latest.get("annual_observation_count"),
    })


def _list_change_summary_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_stability_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_list_changes", "Recommendation List Changes", "no list change data"), "metrics": []}
    df = df.sort_values("trade_date").tail(120)
    fig, ax1 = plt.subplots(figsize=(10, 5))
    x = pd.to_datetime(df["trade_date"])
    ax1.plot(x, pd.to_numeric(df.get("selected_count", df.get("selected_count_current")), errors="coerce"), label="selected_count")
    ax1.plot(x, pd.to_numeric(df["new_entry_count"], errors="coerce"), label="new_entry_count")
    ax1.plot(x, pd.to_numeric(df["dropout_count"], errors="coerce"), label="dropout_count")
    ax2 = ax1.twinx()
    ax2.plot(x, pd.to_numeric(df.get("overlap_ratio_current"), errors="coerce"), linestyle="--", label="overlap_ratio")
    ax2.plot(x, pd.to_numeric(df.get("weight_turnover_1d"), errors="coerce"), linestyle=":", label="weight_turnover")
    latest = df.iloc[-1]
    ax1.set_title(f"Recommendation List Changes ({latest.get('run_mode')}, {latest.get('selector_name')})")
    ax1.legend(loc="upper left")
    ax2.legend(loc="upper right")
    path = _save(fig, chart_root, trade_date, "recommendation_list_changes", plt)
    return _result(trade_date, "recommendation_list_changes", "Recommendation List Changes", path, {
        "selected_count": latest.get("selected_count", latest.get("selected_count_current")),
        "new_entry_count": latest.get("new_entry_count"),
        "dropout_count": latest.get("dropout_count"),
        "overlap_ratio": latest.get("overlap_ratio_current"),
        "weight_turnover": latest.get("weight_turnover_1d"),
    })


def _evaluation_hit_rate_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_evaluation_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_hit_rate", "Recommendation Hit Rate", "no evaluation data"), "metrics": []}
    df = df.sort_values("evaluation_date").tail(120)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(pd.to_datetime(df["evaluation_date"]), pd.to_numeric(df["positive_hit_rate"], errors="coerce"), label="positive_hit_rate")
    ax.set_title("Recommendation Positive Hit Rate")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "recommendation_hit_rate", plt)
    return _result(trade_date, "recommendation_hit_rate", "Recommendation Positive Hit Rate", path, {
        "latest_positive_hit_rate": df["positive_hit_rate"].iloc[-1],
        "completed_stock_count": df["completed_stock_count"].iloc[-1],
    })


def _stability_overlap_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_stability_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_topn_overlap", "Recommendation TopN Overlap", "no stability data"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    for version, group in df.sort_values("trade_date").groupby("recommendation_version"):
        y = pd.to_numeric(group.get("overlap_ratio_current", group.get("overlap_ratio_1d")), errors="coerce")
        ax.plot(pd.to_datetime(group["trade_date"]), y, label=str(version))
    ax.set_title("TopN 1D Overlap Ratio")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "recommendation_topn_overlap", plt)
    return _result(trade_date, "recommendation_topn_overlap", "Recommendation TopN Overlap", path, {"latest_overlap_ratio_current": df.get("overlap_ratio_current", df["overlap_ratio_1d"]).dropna().iloc[-1] if df.get("overlap_ratio_current", df["overlap_ratio_1d"]).dropna().size else None})


def _stability_turnover_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_stability_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_turnover", "Recommendation Turnover", "no stability data"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    for version, group in df.sort_values("trade_date").groupby("recommendation_version"):
        y = pd.to_numeric(group.get("weight_turnover_1d", group.get("turnover_1d")), errors="coerce")
        ax.plot(pd.to_datetime(group["trade_date"]), y, label=str(version))
    ax.set_title("TopN 1D Turnover")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "recommendation_turnover", plt)
    turnover_col = df.get("weight_turnover_1d", df["turnover_1d"])
    return _result(trade_date, "recommendation_turnover", "Recommendation Turnover", path, {"latest_weight_turnover_1d": turnover_col.dropna().iloc[-1] if turnover_col.dropna().size else None})


def _stability_entry_dropout_chart(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_stability_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_entry_dropout", "Recommendation Entry Dropout", "no stability data"), "metrics": []}
    latest_version = str(df.sort_values("trade_date")["recommendation_version"].iloc[-1])
    group = df[df["recommendation_version"].astype(str) == latest_version].sort_values("trade_date").tail(90)
    fig, ax = plt.subplots(figsize=(10, 5))
    x = pd.to_datetime(group["trade_date"])
    ax.plot(x, pd.to_numeric(group["new_entry_count"], errors="coerce"), label="new_entry_count")
    ax.plot(x, pd.to_numeric(group["dropout_count"], errors="coerce"), label="dropout_count")
    ax.set_title(f"New Entries and Dropouts ({latest_version})")
    ax.legend()
    path = _save(fig, chart_root, trade_date, "recommendation_entry_dropout", plt)
    return _result(trade_date, "recommendation_entry_dropout", "Recommendation New Entries and Dropouts", path, {"latest_new_entry_count": group["new_entry_count"].iloc[-1], "latest_dropout_count": group["dropout_count"].iloc[-1]})


def _consecutive_days_distribution(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_stability_daily")
    if df.empty:
        return {"snapshot": _warning(trade_date, "recommendation_consecutive_days", "Recommendation Consecutive Days", "no stability data"), "metrics": []}
    values = pd.to_numeric(df["mean_consecutive_days"], errors="coerce").dropna()
    if values.empty:
        return {"snapshot": _warning(trade_date, "recommendation_consecutive_days", "Recommendation Consecutive Days", "no consecutive-day values"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(values, bins=min(20, max(5, len(values))))
    ax.set_title("Mean Consecutive Days Distribution")
    path = _save(fig, chart_root, trade_date, "recommendation_consecutive_days", plt)
    return _result(trade_date, "recommendation_consecutive_days", "Recommendation Consecutive Days", path, {"mean_consecutive_days": float(values.mean())})


def _strategy_return_comparison(store: AnalysisLakeStore, chart_root: Path, trade_date: str, plt):
    df = store.read_dataset("recommendation_batch_result")
    if df.empty or "recommendation_version" not in df:
        return {"snapshot": _warning(trade_date, "recommendation_strategy_comparison", "Recommendation Strategy Comparison", "no batch result data"), "metrics": []}
    grouped = df.groupby("recommendation_version", as_index=False).agg(
        weighted_net_return=("weighted_net_return", "mean"),
        portfolio_max_drawdown=("portfolio_max_drawdown", "mean"),
    )
    if grouped.empty:
        return {"snapshot": _warning(trade_date, "recommendation_strategy_comparison", "Recommendation Strategy Comparison", "no grouped strategy data"), "metrics": []}
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(grouped["recommendation_version"].astype(str), pd.to_numeric(grouped["weighted_net_return"], errors="coerce"))
    ax.set_title("raw_top_n vs confirmed_top_n vs low_turnover_top_n")
    ax.tick_params(axis="x", rotation=25)
    path = _save(fig, chart_root, trade_date, "recommendation_strategy_comparison", plt)
    return _result(trade_date, "recommendation_strategy_comparison", "Recommendation Strategy Comparison", path, {"strategy_count": len(grouped)})


def _save(fig, chart_root: Path, trade_date: str, chart_type: str, plt) -> Path:
    path = chart_root / f"{trade_date}_{chart_type}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _result(trade_date: str, chart_type: str, title: str, path: Path, metric_values: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "chart_id": f"{trade_date}_{chart_type}",
        "chart_type": chart_type,
        "trade_date": pd.to_datetime(trade_date).date(),
        "chart_title": title,
        "image_path": str(path),
        "summary_json": json.dumps(metric_values, ensure_ascii=False, default=str),
        "created_at": pd.Timestamp.now(),
    }
    metrics = [
        {
            "metric_name": name,
            "trade_date": pd.to_datetime(trade_date).date(),
            "group_name": chart_type,
            "metric_value": value,
            "metric_version": "recommendation_v1",
            "created_at": pd.Timestamp.now(),
        }
        for name, value in metric_values.items()
        if value is not None and not pd.isna(value)
    ]
    return {"snapshot": snapshot, "metrics": metrics}


def _warning(trade_date: str, chart_type: str, title: str, message: str) -> dict[str, Any]:
    return {
        "chart_id": f"{trade_date}_{chart_type}",
        "chart_type": chart_type,
        "trade_date": pd.to_datetime(trade_date).date(),
        "chart_title": title,
        "image_path": "",
        "summary_json": json.dumps({"status": "WARNING", "message": message}, ensure_ascii=False),
        "created_at": pd.Timestamp.now(),
    }
