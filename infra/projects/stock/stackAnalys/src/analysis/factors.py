from __future__ import annotations

import pandas as pd

from src.analysis.indicators import add_basic_indicators


def calc_latest_factors(price_df: pd.DataFrame) -> pd.DataFrame:
    if price_df.empty:
        return pd.DataFrame()
    rows = []
    for ts_code, group in price_df.sort_values(["ts_code", "trade_date"]).groupby("ts_code", sort=False):
        enriched = add_basic_indicators(group)
        if enriched.empty:
            continue
        latest = enriched.iloc[-1]
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": latest.get("trade_date"),
                "close": latest.get("close"),
                "momentum_20d": latest.get("ret_20d"),
                "momentum_60d": latest.get("ret_60d"),
                "momentum_120d": latest.get("ret_120d"),
                "volatility_20d": latest.get("volatility_20d"),
                "amount_ma_20": latest.get("amount_ma_20"),
                "drawdown_60d": latest.get("max_drawdown_60d"),
                "trend_ma20": bool(latest.get("above_ma20")),
                "trend_ma60": bool(latest.get("above_ma60")),
                "source": latest.get("source") if "source" in enriched else None,
                "history_count": len(enriched),
            }
        )
    return pd.DataFrame(rows)

