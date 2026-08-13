from __future__ import annotations

import pandas as pd

from src.analysis.indicators import add_basic_indicators
from src.analysis.scoring import score_stocks


def backtest_monthly_top_n(
    price_df: pd.DataFrame,
    top_n: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
    min_holding_days: int = 10,
) -> dict[str, pd.DataFrame | dict[str, float | int | None]]:
    if price_df.empty:
        raise RuntimeError("No price data for backtest.")
    data = price_df.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    if start_date:
        data = data[data["trade_date"] >= pd.to_datetime(start_date)]
    if end_date:
        data = data[data["trade_date"] <= pd.to_datetime(end_date)]
    if data["trade_date"].nunique() < 40:
        raise RuntimeError("Not enough trading days for monthly backtest.")

    all_data = price_df.copy()
    all_data["trade_date"] = pd.to_datetime(all_data["trade_date"])
    enriched = _enrich_market(all_data)
    rebal_dates = data.groupby(data["trade_date"].dt.to_period("M"))["trade_date"].max().sort_values().tolist()
    monthly_returns = []
    holdings = []
    equity = 1.0
    curve = []
    for current, nxt in zip(rebal_dates[:-1], rebal_dates[1:]):
        if (pd.Timestamp(nxt) - pd.Timestamp(current)).days < min_holding_days:
            continue
        snapshot = _factor_snapshot(enriched, current)
        scored = score_stocks(snapshot).head(top_n)
        selected = scored["ts_code"].tolist()
        if not selected:
            continue
        start_px = _price_on_or_before(all_data, selected, current)
        end_px = _price_on_or_before(all_data, selected, nxt)
        merged = start_px.merge(end_px, on="ts_code", suffixes=("_start", "_end"))
        merged["ret"] = merged["close_end"] / merged["close_start"] - 1
        month_ret = float(merged["ret"].mean()) if not merged.empty else 0.0
        equity *= 1 + month_ret
        monthly_returns.append({"period": str(pd.Period(current, "M")), "rebalance_date": current.date(), "next_date": nxt.date(), "return": month_ret, "equity": equity})
        curve.append({"trade_date": nxt.date(), "equity": equity})
        for code in selected:
            holdings.append({"period": str(pd.Period(current, "M")), "rebalance_date": current.date(), "ts_code": code})
    monthly_df = pd.DataFrame(monthly_returns)
    equity_df = pd.DataFrame(curve)
    holdings_df = pd.DataFrame(holdings)
    summary = _summary(monthly_df)
    return {"equity_curve": equity_df, "monthly_returns": monthly_df, "holdings": holdings_df, "summary": summary}


def _enrich_market(data: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for _, group in data.sort_values(["ts_code", "trade_date"]).groupby("ts_code", sort=False):
        enriched = add_basic_indicators(group)
        enriched["history_count"] = range(1, len(enriched) + 1)
        frames.append(enriched)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _factor_snapshot(enriched: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
    rows = enriched[enriched["trade_date"] <= date].sort_values(["ts_code", "trade_date"]).groupby("ts_code", as_index=False).tail(1)
    if rows.empty:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "ts_code": rows["ts_code"].values,
            "trade_date": rows["trade_date"].values,
            "close": rows["close"].values,
            "momentum_20d": rows.get("ret_20d").values,
            "momentum_60d": rows.get("ret_60d").values,
            "momentum_120d": rows.get("ret_120d").values,
            "volatility_20d": rows.get("volatility_20d").values,
            "amount_ma_20": rows.get("amount_ma_20").values,
            "drawdown_60d": rows.get("max_drawdown_60d").values,
            "trend_ma20": rows.get("above_ma20").values,
            "trend_ma60": rows.get("above_ma60").values,
            "source": rows["source"].values if "source" in rows else None,
            "history_count": rows["history_count"].values if "history_count" in rows else None,
        }
    )


def _price_on_or_before(data: pd.DataFrame, symbols: list[str], date: pd.Timestamp) -> pd.DataFrame:
    subset = data[(data["ts_code"].isin(symbols)) & (data["trade_date"] <= date)].sort_values(["ts_code", "trade_date"])
    return subset.groupby("ts_code", as_index=False).tail(1)[["ts_code", "close"]]


def _summary(monthly: pd.DataFrame) -> dict[str, float | int | None]:
    if monthly.empty:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0, "win_rate": 0.0, "month_count": 0, "avg_monthly_return": None, "best_month": None, "worst_month": None}
    returns = pd.to_numeric(monthly["return"], errors="coerce")
    equity = pd.to_numeric(monthly["equity"], errors="coerce")
    month_count = len(monthly)
    total_return = float(equity.iloc[-1] - 1)
    annual_return = float(equity.iloc[-1] ** (12 / month_count) - 1) if month_count else 0.0
    drawdown = equity / equity.cummax() - 1
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": float(drawdown.min()),
        "win_rate": float((returns > 0).mean()),
        "month_count": int(month_count),
        "avg_monthly_return": float(returns.mean()),
        "best_month": float(returns.max()),
        "worst_month": float(returns.min()),
    }
