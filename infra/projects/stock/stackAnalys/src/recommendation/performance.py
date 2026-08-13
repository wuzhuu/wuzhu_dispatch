from __future__ import annotations

import pandas as pd


MONTHLY_WINDOW = 20
ANNUAL_WINDOW = 252


def calculate_rolling_returns(portfolio: pd.DataFrame, min_annualized_days: int = 60) -> list[dict]:
    if portfolio.empty:
        return []
    df = portfolio.copy()
    df["trading_date"] = pd.to_datetime(df["trading_date"], errors="coerce")
    df = df.dropna(subset=["trading_date"]).sort_values("trading_date")
    if "benchmark_equity" not in df:
        if "benchmark_daily_return" in df:
            df["benchmark_equity"] = pd.NA
        elif "benchmark_cumulative_return" in df:
            df["benchmark_equity"] = 1 + pd.to_numeric(df["benchmark_cumulative_return"], errors="coerce")
        else:
            df["benchmark_equity"] = pd.NA
    for col in ("run_mode", "recommendation_version", "selector_name"):
        if col not in df:
            df[col] = "live_shadow" if col == "run_mode" else ""
        df[col] = df[col].fillna("live_shadow" if col == "run_mode" else "").astype(str)

    rows: list[dict] = []
    for keys, group in df.groupby(["run_mode", "recommendation_version", "selector_name"], dropna=False):
        group = group.sort_values("trading_date").reset_index(drop=True)
        for col, daily_col in (("gross_equity", "gross_daily_return"), ("net_equity", "net_daily_return"), ("benchmark_equity", "benchmark_daily_return")):
            if col not in group or group[col].isna().all():
                if daily_col in group:
                    daily = pd.to_numeric(group[daily_col], errors="coerce").fillna(0.0)
                    group[col] = (1 + daily).cumprod()
        run_mode, version, selector = keys
        for idx, row in group.iterrows():
            monthly = _window_return(group, idx, MONTHLY_WINDOW)
            annual = _window_return(group, idx, ANNUAL_WINDOW)
            annualized = _annualized_to_date(group, idx, min_annualized_days)
            rows.append({
                "trade_date": row["trading_date"].date(),
                "trading_date": row["trading_date"].date(),
                "run_mode": str(run_mode or "live_shadow"),
                "recommendation_version": str(version or ""),
                "selector_name": str(selector or ""),
                "rolling_monthly_gross_return": monthly["gross"],
                "rolling_monthly_net_return": monthly["net"],
                "rolling_monthly_benchmark_return": monthly["benchmark"],
                "rolling_monthly_excess_return": _sub(monthly["net"], monthly["benchmark"]),
                "monthly_window_start": monthly["start"],
                "monthly_window_end": monthly["end"],
                "monthly_observation_count": monthly["count"],
                "monthly_return_status": monthly["status"],
                "rolling_annual_gross_return": annual["gross"],
                "rolling_annual_net_return": annual["net"],
                "rolling_annual_benchmark_return": annual["benchmark"],
                "rolling_annual_excess_return": _sub(annual["net"], annual["benchmark"]),
                "annualized_return_to_date": annualized,
                "annual_window_start": annual["start"],
                "annual_window_end": annual["end"],
                "annual_observation_count": annual["count"],
                "annual_return_status": annual["status"],
                "quality_status": "WARNING" if monthly["status"] != "OK" or annual["status"] != "OK" else "OK",
                "created_at": pd.Timestamp.now(),
            })
    return rows


def _window_return(group: pd.DataFrame, idx: int, window: int) -> dict:
    if idx < window:
        return {
            "gross": None,
            "net": None,
            "benchmark": None,
            "start": None,
            "end": group.loc[idx, "trading_date"].date(),
            "count": int(idx),
            "status": "INSUFFICIENT_HISTORY",
        }
    start = group.loc[idx - window]
    end = group.loc[idx]
    gross = _ratio(end.get("gross_equity"), start.get("gross_equity"))
    net = _ratio(end.get("net_equity"), start.get("net_equity"))
    benchmark = _ratio(end.get("benchmark_equity"), start.get("benchmark_equity"))
    return {
        "gross": gross,
        "net": net,
        "benchmark": benchmark,
        "start": start["trading_date"].date(),
        "end": end["trading_date"].date(),
        "count": int(window),
        "status": "OK",
    }


def _annualized_to_date(group: pd.DataFrame, idx: int, min_days: int) -> float | None:
    if idx < min_days:
        return None
    start = group.loc[0]
    end = group.loc[idx]
    total = _ratio(end.get("net_equity"), start.get("net_equity"))
    if total is None:
        return None
    return float((1 + total) ** (252 / idx) - 1)


def _ratio(end, start) -> float | None:
    try:
        end_f = float(end)
        start_f = float(start)
    except (TypeError, ValueError):
        return None
    if pd.isna(end_f) or pd.isna(start_f) or start_f == 0:
        return None
    return end_f / start_f - 1


def _sub(left, right) -> float | None:
    if left is None or right is None or pd.isna(left) or pd.isna(right):
        return None
    return float(left - right)
