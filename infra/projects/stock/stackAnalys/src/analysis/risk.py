from __future__ import annotations

import pandas as pd


LOW_LIQUIDITY_AMOUNT = 30_000_000
HIGH_VOLATILITY_20D = 0.60
LARGE_DRAWDOWN_60D = -0.25
MIN_HISTORY_DAYS = 120


def add_risk_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    flags_col = []
    levels = []
    for _, row in out.iterrows():
        flags: list[str] = []
        amount = pd.to_numeric(row.get("amount_ma_20"), errors="coerce")
        volatility = pd.to_numeric(row.get("volatility_20d"), errors="coerce")
        drawdown = pd.to_numeric(row.get("drawdown_60d"), errors="coerce")
        history_count = pd.to_numeric(row.get("history_count"), errors="coerce")
        if pd.notna(amount) and amount < LOW_LIQUIDITY_AMOUNT:
            flags.append("low_liquidity")
        if pd.notna(volatility) and volatility > HIGH_VOLATILITY_20D:
            flags.append("high_volatility")
        if pd.notna(drawdown) and drawdown < LARGE_DRAWDOWN_60D:
            flags.append("large_drawdown")
        if not bool(row.get("trend_ma20", False)) or not bool(row.get("trend_ma60", False)):
            flags.append("weak_trend")
        if pd.notna(history_count) and history_count < MIN_HISTORY_DAYS:
            flags.append("insufficient_history")
        flags_col.append(",".join(flags))
        levels.append("HIGH" if len(flags) >= 3 else "MEDIUM" if flags else "LOW")
    out["risk_flags"] = flags_col
    out["risk_level"] = levels
    return out

