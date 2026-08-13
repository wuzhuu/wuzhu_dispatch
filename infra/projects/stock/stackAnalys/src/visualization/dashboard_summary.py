from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.indicators import add_basic_indicators
from src.visualization.chart_data import VisualContext, build_context


def build_visual_dashboard(db_path: str | Path | None = None, target_date: str | None = None) -> dict[str, Any]:
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

    ctx = build_context(db_path, target_date)
    snapshots: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        chart_builders = [
            _market_breadth,
            _hs300_trend,
            _backtest_equity_curve,
            _backtest_drawdown,
            _monthly_return_heatmap,
            _risk_distribution,
            _top20_industry_distribution,
            _top20_risk_distribution,
            _ranking_change_summary,
            _score_decile_forward_return,
        ]
        for builder in chart_builders:
            try:
                result = builder(ctx, plt)
                if result is None:
                    continue
                snapshots.append(result["snapshot"])
                metrics.extend(result.get("metrics", []))
                print(f"chart: {result['snapshot']['chart_type']} -> {result['snapshot']['image_path']}")
            except Exception as exc:
                warning = f"{builder.__name__}: {exc!r}"
                warnings.append(warning)
                print(f"WARNING {warning}")
        ctx.loader.close()
        if snapshots:
            ctx.store.write_dataset("visualization_snapshot", pd.DataFrame(snapshots))
        if metrics:
            ctx.store.write_dataset("visualization_metric", pd.DataFrame(metrics))
        print(f"charts_dir: {ctx.chart_root}")
        return {"snapshots": snapshots, "metrics": metrics, "warnings": warnings, "data_root": str(ctx.store.data_root)}
    finally:
        ctx.store.close()
        try:
            ctx.loader.close()
        except Exception:
            pass


def _market_breadth(ctx: VisualContext, plt):
    state = ctx.store.read_dataset("market_state_daily")
    if state.empty:
        return None
    state = state.sort_values("trade_date").tail(120)
    fig, ax = plt.subplots(figsize=(10, 5))
    x = pd.to_datetime(state["trade_date"])
    ax.plot(x, pd.to_numeric(state.get("above_ma20_ratio"), errors="coerce"), label="above_ma20_ratio")
    ax.plot(x, pd.to_numeric(state.get("above_ma60_ratio"), errors="coerce"), label="above_ma60_ratio")
    ax.set_title("Market Breadth")
    ax.legend()
    path = _save(fig, ctx, "market_breadth", plt)
    latest = state.iloc[-1]
    return _result(ctx, "market_breadth", "Market Breadth", path, {
        "above_ma20_ratio": latest.get("above_ma20_ratio"),
        "above_ma60_ratio": latest.get("above_ma60_ratio"),
        "stock_count": latest.get("stock_count"),
    })


def _hs300_trend(ctx: VisualContext, plt):
    df = ctx.loader.get_index_price("sh.000300", start_date="2025-01-01", end_date=ctx.trade_date)
    if df.empty or len(df) < 20:
        print("WARNING hs300_trend: index_daily history is insufficient")
        return _warning_result(ctx, "hs300_trend", "HS300 Trend", "index_daily history is insufficient")
    enriched = add_basic_indicators(df).tail(180)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(pd.to_datetime(enriched["trade_date"]), enriched["close"], label="close")
    ax.plot(pd.to_datetime(enriched["trade_date"]), enriched["ma_20"], label="ma20")
    ax.plot(pd.to_datetime(enriched["trade_date"]), enriched["ma_60"], label="ma60")
    ax.set_title("HS300 Trend")
    ax.legend()
    path = _save(fig, ctx, "hs300_trend", plt)
    return _result(ctx, "hs300_trend", "HS300 Trend", path, {})


def _backtest_equity_curve(ctx: VisualContext, plt):
    monthly = _latest_backtest_monthly(ctx)
    if monthly.empty:
        return None
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(pd.to_datetime(monthly["next_date"]), monthly["equity"], label="strategy")
    ax.axhline(1.0, color="gray", linewidth=1, label="benchmark")
    ax.plot(pd.to_datetime(monthly["next_date"]), monthly["equity"] - 1.0, label="excess")
    ax.set_title("Backtest Equity Curve")
    ax.legend()
    path = _save(fig, ctx, "backtest_equity_curve", plt)
    latest = ctx.store.read_dataset("backtest_result").sort_values("created_at").iloc[-1]
    return _result(ctx, "backtest_equity_curve", "Backtest Equity Curve", path, {
        "strategy_total_return": latest.get("total_return"),
        "strategy_annual_return": latest.get("annual_return"),
        "benchmark_return": latest.get("benchmark_return"),
        "excess_return": latest.get("excess_return"),
    })


def _backtest_drawdown(ctx: VisualContext, plt):
    monthly = _latest_backtest_monthly(ctx)
    if monthly.empty:
        return None
    equity = pd.to_numeric(monthly["equity"], errors="coerce")
    drawdown = equity / equity.cummax() - 1
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(pd.to_datetime(monthly["next_date"]), drawdown, label="drawdown")
    ax.set_title("Backtest Drawdown")
    ax.legend()
    path = _save(fig, ctx, "backtest_drawdown", plt)
    return _result(ctx, "backtest_drawdown", "Backtest Drawdown", path, {"strategy_max_drawdown": drawdown.min()})


def _monthly_return_heatmap(ctx: VisualContext, plt):
    monthly = _latest_backtest_monthly(ctx)
    if monthly.empty:
        return None
    data = monthly.copy()
    data["date"] = pd.to_datetime(data["period"].astype(str) + "-01")
    pivot = data.pivot_table(index=data["date"].dt.year, columns=data["date"].dt.month, values="return", aggfunc="mean")
    fig, ax = plt.subplots(figsize=(10, 5))
    image = ax.imshow(pivot.fillna(0), aspect="auto", cmap="RdYlGn")
    ax.set_title("Monthly Return Heatmap")
    ax.set_yticks(range(len(pivot.index)), labels=pivot.index)
    ax.set_xticks(range(12), labels=list(range(1, 13)))
    fig.colorbar(image, ax=ax)
    path = _save(fig, ctx, "monthly_return_heatmap", plt)
    return _result(ctx, "monthly_return_heatmap", "Monthly Return Heatmap", path, {})


def _risk_distribution(ctx: VisualContext, plt):
    risk = _latest_by_date(ctx.store.read_dataset("risk_flag_daily"), ctx.trade_date)
    if risk.empty:
        return None
    counts = risk["risk_level"].fillna("UNKNOWN").value_counts()
    fig, ax = plt.subplots(figsize=(7, 5))
    _plot_pie(counts, ax)
    ax.set_title("Risk Distribution")
    path = _save(fig, ctx, "risk_distribution", plt)
    flag_counts = risk["risk_flags"].fillna("").astype(str).str.split(",").explode()
    flag_counts = flag_counts[flag_counts != ""].value_counts()
    metrics = {f"{level.lower()}_risk_count": counts.get(level, 0) for level in ("HIGH", "MEDIUM", "LOW")}
    metrics.update({f"{flag}_count": value for flag, value in flag_counts.items()})
    return _result(ctx, "risk_distribution", "Risk Distribution", path, metrics)


def _top20_industry_distribution(ctx: VisualContext, plt):
    score = _latest_by_date(ctx.store.read_dataset("score_daily"), ctx.trade_date)
    if score.empty or "industry" not in score.columns:
        return _warning_result(ctx, "top20_industry_distribution", "Top20 Industry Distribution", "industry column is missing")
    top20 = score.sort_values("rank").head(20)
    missing_ratio = float(top20["industry"].isna().mean())
    if missing_ratio > 0.8:
        print("WARNING top20_industry_distribution: industry missing ratio too high")
        return _warning_result(ctx, "top20_industry_distribution", "Top20 Industry Distribution", "industry missing ratio too high")
    counts = _merge_small_slices(top20["industry"].fillna("UNKNOWN").value_counts(), max_slices=6)
    fig, ax = plt.subplots(figsize=(8, 6))
    _plot_pie(counts, ax)
    ax.set_title("Top20 Industry Distribution")
    path = _save(fig, ctx, "top20_industry_distribution", plt)
    return _result(ctx, "top20_industry_distribution", "Top20 Industry Distribution", path, {})


def _top20_risk_distribution(ctx: VisualContext, plt):
    score = _latest_by_date(ctx.store.read_dataset("score_daily"), ctx.trade_date)
    if score.empty or "risk_level" not in score.columns:
        return None
    top20 = score.sort_values("rank").head(20)
    counts = top20["risk_level"].fillna("UNKNOWN").value_counts()
    fig, ax = plt.subplots(figsize=(7, 5))
    _plot_pie(counts, ax)
    ax.set_title("Top20 Risk Distribution")
    path = _save(fig, ctx, "top20_risk_distribution", plt)
    return _result(ctx, "top20_risk_distribution", "Top20 Risk Distribution", path, {
        "top20_low_risk_count": counts.get("LOW", 0),
        "top20_high_risk_count": counts.get("HIGH", 0),
        "top_score_mean": pd.to_numeric(top20.get("total_score"), errors="coerce").mean(),
    })


def _ranking_change_summary(ctx: VisualContext, plt):
    change = _latest_by_date(ctx.store.read_dataset("ranking_change_daily"), ctx.trade_date)
    if change.empty:
        return None
    new_count = int(change.get("is_new_top", pd.Series(dtype=bool)).fillna(False).sum())
    drop_count = int(change.get("is_drop_top", pd.Series(dtype=bool)).fillna(False).sum())
    top = change.assign(abs_change=pd.to_numeric(change.get("rank_change"), errors="coerce").abs()).sort_values("abs_change", ascending=False).head(10)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(top["ts_code"], pd.to_numeric(top.get("rank_change"), errors="coerce").fillna(0))
    ax.set_title("Ranking Change Summary")
    ax.tick_params(axis="x", rotation=45)
    path = _save(fig, ctx, "ranking_change_summary", plt)
    return _result(ctx, "ranking_change_summary", "Ranking Change Summary", path, {
        "new_top20_count": new_count,
        "drop_top20_count": drop_count,
        "top20_overlap_ratio": None,
    })


def _score_decile_forward_return(ctx: VisualContext, plt):
    score = _latest_score_with_forward_window(ctx)
    if score.empty:
        print("WARNING score_decile_forward_return: no score date has enough future price data")
        return _warning_result(ctx, "score_decile_forward_return", "Score Decile Forward Return", "no score date has enough future price data")
    score_date = str(pd.to_datetime(score["trade_date"].iloc[0]).date())
    price = ctx.loader.get_market_price(start_date=score_date)
    if price.empty:
        return None
    returns = _forward_returns(score, price)
    if returns.empty:
        return None
    returns["decile"] = pd.qcut(pd.to_numeric(returns["total_score"], errors="coerce"), 10, duplicates="drop")
    grouped = returns.groupby("decile", observed=True)["forward_20d_return"].mean()
    fig, ax = plt.subplots(figsize=(10, 5))
    grouped.plot(kind="bar", ax=ax)
    ax.set_title("Score Decile Forward Return")
    path = _save(fig, ctx, "score_decile_forward_return", plt)
    return _result(ctx, "score_decile_forward_return", "Score Decile Forward Return", path, {})


def _forward_returns(score: pd.DataFrame, price: pd.DataFrame) -> pd.DataFrame:
    rows = []
    price = price.copy()
    price["trade_date"] = pd.to_datetime(price["trade_date"])
    for _, row in score.iterrows():
        ts_code = row["ts_code"]
        hist = price[price["ts_code"] == ts_code].sort_values("trade_date")
        if len(hist) < 21:
            continue
        start = hist.iloc[0]["close"]
        end = hist.iloc[20]["close"]
        rows.append({"ts_code": ts_code, "total_score": row.get("total_score"), "forward_20d_return": end / start - 1})
    return pd.DataFrame(rows)


def _latest_backtest_monthly(ctx: VisualContext) -> pd.DataFrame:
    monthly = ctx.store.read_dataset("backtest_monthly_returns")
    if not monthly.empty:
        latest_run = monthly.sort_values("created_at").iloc[-1].get("run_id") if "created_at" in monthly.columns else monthly.iloc[-1].get("run_id")
        return monthly[monthly["run_id"] == latest_run].sort_values("rebalance_date")
    result = ctx.store.read_dataset("backtest_result")
    if result.empty or "summary_json" not in result.columns:
        return pd.DataFrame()
    row = result.sort_values("created_at").iloc[-1]
    payload = json.loads(row["summary_json"])
    return pd.DataFrame(payload.get("monthly_returns", []))


def _latest_score_with_forward_window(ctx: VisualContext) -> pd.DataFrame:
    scores = ctx.store.read_dataset("score_daily")
    if scores.empty or "trade_date" not in scores.columns:
        return pd.DataFrame()
    scores = scores.copy()
    scores["trade_date"] = pd.to_datetime(scores["trade_date"], errors="coerce").dt.date
    for date_value in sorted(scores["trade_date"].dropna().unique(), reverse=True):
        price = ctx.loader.get_market_price(start_date=str(date_value))
        if price.empty:
            continue
        counts = price.groupby("ts_code").size()
        if (counts >= 21).any():
            return scores[scores["trade_date"] == date_value]
    return pd.DataFrame()


def _plot_pie(counts: pd.Series, ax) -> None:
    counts = counts[counts > 0]
    counts.plot(kind="pie", ax=ax, autopct="%1.1f%%", startangle=90, counterclock=False)
    ax.set_ylabel("")


def _merge_small_slices(counts: pd.Series, max_slices: int = 6) -> pd.Series:
    if len(counts) <= max_slices:
        return counts
    keep = counts.head(max_slices - 1).copy()
    other = counts.iloc[max_slices - 1 :].sum()
    if other > 0:
        keep.loc["OTHER"] = other
    return keep


def _latest_by_date(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if df.empty or "trade_date" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
    target = pd.to_datetime(trade_date).date()
    subset = out[out["trade_date"] == target]
    return subset if not subset.empty else out[out["trade_date"] == out["trade_date"].max()]


def _save(fig, ctx: VisualContext, chart_type: str, plt) -> Path:
    path = ctx.chart_root / f"{ctx.trade_date}_{chart_type}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _result(ctx: VisualContext, chart_type: str, title: str, path: Path, metric_values: dict[str, Any]) -> dict[str, Any]:
    chart_id = f"{ctx.trade_date}_{chart_type}"
    snapshot = {
        "chart_id": chart_id,
        "chart_type": chart_type,
        "trade_date": ctx.trade_date,
        "chart_title": title,
        "image_path": str(path),
        "summary_json": json.dumps(metric_values, ensure_ascii=False, default=str),
        "created_at": pd.Timestamp.now(),
    }
    metrics = [
        {
            "metric_name": name,
            "trade_date": ctx.trade_date,
            "group_name": chart_type,
            "metric_value": value,
            "metric_version": "v1",
            "created_at": pd.Timestamp.now(),
        }
        for name, value in metric_values.items()
        if value is not None and not pd.isna(value)
    ]
    return {"snapshot": snapshot, "metrics": metrics}


def _warning_result(ctx: VisualContext, chart_type: str, title: str, message: str) -> dict[str, Any]:
    snapshot = {
        "chart_id": f"{ctx.trade_date}_{chart_type}",
        "chart_type": chart_type,
        "trade_date": ctx.trade_date,
        "chart_title": title,
        "image_path": "",
        "summary_json": json.dumps({"status": "WARNING", "message": message}, ensure_ascii=False),
        "created_at": pd.Timestamp.now(),
    }
    return {"snapshot": snapshot, "metrics": []}
