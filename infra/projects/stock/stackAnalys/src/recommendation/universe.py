from __future__ import annotations

import pandas as pd


def normalize_0_1(series: pd.Series, neutral: float = 0.5) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if values.notna().sum() == 0:
        return pd.Series([neutral] * len(series), index=series.index)
    lo = values.min()
    hi = values.max()
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series([neutral] * len(series), index=series.index)
    return ((values - lo) / (hi - lo)).fillna(neutral)


def risk_score(level: object) -> float:
    return {"LOW": 1.0, "MEDIUM": 0.65, "HIGH": 0.15}.get(str(level or "").upper(), 0.5)


def is_st_name(name: object) -> bool:
    text = str(name or "").upper()
    return "ST" in text or "退" in text
