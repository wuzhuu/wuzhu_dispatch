from __future__ import annotations

import pandas as pd


def _rank_score(series: pd.Series, ascending: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == 0:
        return pd.Series(50.0, index=series.index)
    scores = numeric.rank(pct=True, ascending=ascending) * 100
    return scores.fillna(scores.median()).fillna(50.0)


def score_stocks(factors: pd.DataFrame) -> pd.DataFrame:
    if factors.empty:
        return factors.copy()
    out = factors.copy()
    out["score_momentum_20d"] = _rank_score(out.get("momentum_20d"), ascending=True)
    out["score_momentum_60d"] = _rank_score(out.get("momentum_60d"), ascending=True)
    out["score_volatility"] = _rank_score(out.get("volatility_20d"), ascending=False)
    out["score_liquidity"] = _rank_score(out.get("amount_ma_20"), ascending=True)
    out["score_drawdown"] = _rank_score(out.get("drawdown_60d"), ascending=True)
    trend_ma20 = out.get("trend_ma20", False).fillna(False).astype(bool)
    trend_ma60 = out.get("trend_ma60", False).fillna(False).astype(bool)
    out["score_trend"] = trend_ma20.astype(float) * 50 + trend_ma60.astype(float) * 50
    out["total_score"] = (
        out["score_momentum_20d"] * 0.20
        + out["score_momentum_60d"] * 0.20
        + out["score_volatility"] * 0.15
        + out["score_liquidity"] * 0.15
        + out["score_drawdown"] * 0.10
        + out["score_trend"] * 0.20
    )
    out = out.sort_values(["total_score", "ts_code"], ascending=[False, True]).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    preferred = [
        "ts_code",
        "trade_date",
        "close",
        "total_score",
        "rank",
        "score_momentum_20d",
        "score_momentum_60d",
        "score_volatility",
        "score_liquidity",
        "score_drawdown",
        "score_trend",
    ]
    return out[preferred + [col for col in out.columns if col not in preferred]]

