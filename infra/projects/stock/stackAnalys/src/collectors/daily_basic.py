from __future__ import annotations

import pandas as pd

from src.collectors.financial_indicators import fetch_financial_indicators_akshare


def fetch_daily_basic(ts_code: str, trade_date: str | None = None) -> pd.DataFrame:
    """Compatibility wrapper for first-phase free financial indicators.

    Tushare daily_basic is not used in phase 1. Free sources provide financial
    indicators on report/quarter cadence rather than exact daily valuation
    fields, so callers should prefer financial_indicators.py for new code.
    """
    df = fetch_financial_indicators_akshare(ts_code)
    if not df.empty and trade_date:
        df["trade_date"] = trade_date
    return df
