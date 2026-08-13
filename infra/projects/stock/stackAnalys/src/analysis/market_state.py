from __future__ import annotations

from typing import Any

import pandas as pd

from src.analysis.data_loader import StockDataLoader
from src.analysis.factors import calc_latest_factors
from src.analysis.indicators import add_basic_indicators


def analyze_market_state(loader: StockDataLoader) -> dict[str, Any]:
    latest = loader.get_latest_trade_date()
    price = loader.get_market_price(start_date="2025-01-01", end_date=latest)
    factors = calc_latest_factors(price)
    stock_count = len(factors)
    above_ma20_ratio = float(factors["trend_ma20"].mean()) if stock_count else None
    above_ma60_ratio = float(factors["trend_ma60"].mean()) if stock_count else None
    median_ret_20d = float(pd.to_numeric(factors["momentum_20d"], errors="coerce").median()) if stock_count else None
    latest_rows = price[pd.to_datetime(price["trade_date"]).dt.date == pd.to_datetime(latest).date()]
    total_amount = float(pd.to_numeric(latest_rows.get("amount"), errors="coerce").sum()) if not latest_rows.empty else None

    hs300 = _load_hs300(loader, latest)
    hs300_close = hs300_above_ma20 = hs300_above_ma60 = None
    if hs300 is not None and not hs300.empty:
        enriched = add_basic_indicators(hs300)
        row = enriched.iloc[-1]
        hs300_close = float(row["close"])
        hs300_above_ma20 = bool(row["above_ma20"])
        hs300_above_ma60 = bool(row["above_ma60"])

    if above_ma20_ratio is not None and above_ma20_ratio > 0.6 and hs300_above_ma20:
        regime = "偏强"
    elif above_ma20_ratio is not None and above_ma20_ratio < 0.4 and hs300_above_ma60 is False:
        regime = "偏弱"
    else:
        regime = "震荡"
    return {
        "latest_trade_date": latest,
        "stock_count": stock_count,
        "above_ma20_ratio": above_ma20_ratio,
        "above_ma60_ratio": above_ma60_ratio,
        "median_ret_20d": median_ret_20d,
        "total_amount": total_amount,
        "hs300_close": hs300_close,
        "hs300_above_ma20": hs300_above_ma20,
        "hs300_above_ma60": hs300_above_ma60,
        "market_regime": regime,
    }


def _load_hs300(loader: StockDataLoader, latest: str) -> pd.DataFrame | None:
    for code in ("sh.000300", "000300.SH", "000300"):
        df = loader.get_index_price(code, start_date="2025-01-01", end_date=latest)
        if not df.empty:
            return df
    return None

