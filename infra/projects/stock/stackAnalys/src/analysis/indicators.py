from __future__ import annotations

import numpy as np
import pandas as pd


def add_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out = out.sort_values("trade_date").reset_index(drop=True)
    close = pd.to_numeric(out["close"], errors="coerce")
    ret_1d = close.pct_change()
    out["ret_1d"] = ret_1d
    for window in (5, 20, 60, 120):
        out[f"ret_{window}d"] = close.pct_change(window)
    for window in (5, 10, 20, 60, 120):
        out[f"ma_{window}"] = close.rolling(window, min_periods=1).mean()
    out["volatility_20d"] = ret_1d.rolling(20, min_periods=5).std() * np.sqrt(252)
    out["volatility_60d"] = ret_1d.rolling(60, min_periods=10).std() * np.sqrt(252)
    out["amount_ma_20"] = pd.to_numeric(out["amount"], errors="coerce").rolling(20, min_periods=1).mean() if "amount" in out else np.nan
    out["volume_ma_20"] = pd.to_numeric(out["volume"], errors="coerce").rolling(20, min_periods=1).mean() if "volume" in out else np.nan
    peak = close.cummax()
    out["drawdown"] = close / peak - 1
    out["max_drawdown_60d"] = out["drawdown"].rolling(60, min_periods=1).min()
    out["above_ma20"] = close > out["ma_20"]
    out["above_ma60"] = close > out["ma_60"]
    return out

